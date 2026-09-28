"""The LLM model row in Global settings: the Refresh list button, the request it queues for the
Research worker, and how the row reports a pending, finished, failed or stuck refresh."""
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from beehive.ai.model_catalog import (
    BUILTIN_MODELS,
    ListedModel,
    RefreshError,
    RefreshStatus,
    claim_refresh,
    complete_refresh,
    fail_refresh,
    load_refresh_state,
    request_refresh,
)
from beehive.ai.model_selection import choose_model, save_model
from beehive.auth.tokens import sign_session_id
from beehive.db.connection import connect, init_schema
from beehive.db.sessions import create_session
from beehive.web.app import create_app
from beehive.web.deps import SESSION_COOKIE_NAME

SECRET = "test-secret-at-least-32-characters-long"
LISTED = [
    ListedModel("auto", "Auto"),
    ListedModel("claude-haiku-4.5", "Claude Haiku 4.5", "enabled"),
    ListedModel("brand-new-9", "Brand New 9", "enabled"),
]


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    conn = connect(path)
    init_schema(conn)
    create_session(conn, "sess1", "csrf1", "2099-01-01T00:00:00")
    conn.close()
    return path


@pytest.fixture
def client(db_path):
    test_client = TestClient(create_app(db_path, session_secret=SECRET), follow_redirects=False)
    test_client.cookies.set(SESSION_COOKIE_NAME, sign_session_id("sess1", SECRET))
    return test_client


def _now():
    return datetime.now(timezone.utc)


def _with_conn(db_path, action):
    conn = connect(db_path)
    try:
        return action(conn)
    finally:
        conn.close()


def _refreshed(db_path, listed=LISTED):
    def refresh(conn):
        request_refresh(conn, _now())
        return complete_refresh(conn, claim_refresh(conn, _now()), _now(), listed)
    return _with_conn(db_path, refresh)


def _model_row(page):
    start = page.index('action="/admin/model"')
    return page[start:page.index("</form>", start)]


def test_the_model_row_offers_a_refresh_and_says_the_list_is_built_in(client):
    row = _model_row(client.get("/admin/?tab=settings").text)

    assert ('<button class="btn" type="submit" formaction="/admin/model/refresh">'
            "Refresh list</button>") in row
    assert "This is the built-in list and may be out of date." in row
    assert "data-refresh-status-url" not in row


def test_refresh_needs_a_session_and_a_csrf_token(client, db_path):
    anonymous = TestClient(create_app(db_path, session_secret=SECRET), follow_redirects=False)

    assert anonymous.post("/admin/model/refresh", data={"csrf_token": "csrf1"}).status_code == 303
    assert anonymous.get("/admin/model/refresh-status").status_code == 303
    assert client.post("/admin/model/refresh", data={"csrf_token": "wrong"}).status_code == 403
    assert _with_conn(db_path, load_refresh_state) is None


def test_refresh_queues_a_request_for_the_research_worker(client, db_path):
    assert client.get("/admin/model/refresh-status").json() == {"phase": "idle"}

    # The button submits the model form, so the picked model rides along and is ignored.
    response = client.post("/admin/model/refresh", data={"csrf_token": "csrf1", "model": "auto"})

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/?tab=settings#interface-ai"
    assert _with_conn(db_path, load_refresh_state).status is RefreshStatus.QUEUED
    assert _with_conn(db_path, lambda conn: choose_model(conn).saved) is None
    status = client.get("/admin/model/refresh-status")
    assert status.json() == {"phase": "pending"}
    assert "no-store" in status.headers["cache-control"]


def test_a_pending_refresh_disables_the_button_and_polls(client):
    client.post("/admin/model/refresh", data={"csrf_token": "csrf1"})

    row = _model_row(client.get("/admin/?tab=settings").text)

    assert 'formaction="/admin/model/refresh" disabled>Refreshing…</button>' in row
    assert ('<p class="st-line st-busy" role="status" '
            'data-refresh-status-url="/admin/model/refresh-status" data-refresh-phase="pending" '
            'data-refresh-done-url="/admin/?tab=settings&amp;model_refreshed=1">'
            "Getting the list from Copilot. This takes about 10\u00a0seconds.</p>") in row


def test_the_refreshed_list_replaces_the_built_in_one(client, db_path):
    _refreshed(db_path)

    row = _model_row(client.get("/admin/?tab=settings").text)

    assert re.findall(r'<option value="([^"]+)"', row) == ["auto", "brand-new-9", "claude-haiku-4.5"]
    assert '<option value="claude-haiku-4.5" selected>' in row
    assert re.search(
        r'3 models from your Copilot account\. Last refresh: <span class="nw">Today \d\d:\d\d</span>\. '
        r"Refreshes daily\.", row)
    assert client.get("/admin/model/refresh-status").json() == {"phase": "done"}


def test_after_the_reload_a_flash_counts_what_changed(client, db_path):
    _refreshed(db_path)
    removed = len(BUILTIN_MODELS) - 2  # every built-in model except auto and claude-haiku-4.5

    assert (f"Model list refreshed: 3 models, 1 new, {removed} removed."
            in client.get("/admin/?tab=settings&model_refreshed=1").text)
    assert "Model list refreshed" not in client.get("/admin/?tab=settings").text

    _refreshed(db_path)
    assert ("Model list refreshed. Nothing changed (3 models)."
            in client.get("/admin/?tab=settings&model_refreshed=1").text)


def test_a_failed_refresh_says_why_and_can_be_retried(client, db_path):
    def fail(conn):
        fail_refresh(conn, claim_refresh(conn, _now()), _now(), RefreshError.FAILED,
                     "RuntimeError: not authenticated")
    _with_conn(db_path, fail)

    row = _model_row(client.get("/admin/?tab=settings").text)

    assert re.search(
        r'<p class="st-line st-error">Refresh failed \(<span class="nw">Today \d\d:\d\d</span>\): the Copilot SDK '
        r"returned an error\. The list above is the previous one\.</p>", row)
    assert ('<details class="raw-error"><summary>Raw error</summary>'
            "<code>RuntimeError: not authenticated</code></details>") in row
    assert 'formaction="/admin/model/refresh">Refresh list</button>' in row
    assert client.get("/admin/model/refresh-status").json() == {"phase": "failed"}


def test_a_request_nobody_picked_up_points_at_the_research_worker(client, db_path):
    _with_conn(db_path, lambda conn: request_refresh(conn, _now() - timedelta(minutes=5)))

    row = _model_row(client.get("/admin/?tab=settings").text)

    assert 'data-refresh-phase="waiting"' in row
    assert "Check that beehive-research.service is running." in row
    assert client.get("/admin/model/refresh-status").json() == {"phase": "waiting"}


def test_a_refresh_a_dead_worker_left_running_reads_as_interrupted(client, db_path):
    _with_conn(db_path, lambda conn: claim_refresh(conn, _now() - timedelta(minutes=10)))

    row = _model_row(client.get("/admin/?tab=settings").text)

    assert "the Research worker stopped before it finished" in row
    assert 'formaction="/admin/model/refresh">Refresh list</button>' in row


def test_a_saved_model_that_left_the_list_is_flagged(client, db_path):
    _with_conn(db_path, lambda conn: save_model(conn, "gpt-5.6-sol"))
    _refreshed(db_path)

    page = client.get("/admin/?tab=settings").text
    row = _model_row(page)

    assert ("gpt-5.6-sol is no longer offered, so AI work uses the default, Claude Haiku 4.5, "
            "until you choose another model.") in row
    assert '<option value="claude-haiku-4.5" selected>' in row
    assert re.search(r'<span>Global settings</span><span class="toc-count"', page)


def test_a_model_from_the_refreshed_list_can_be_saved(client, db_path):
    _refreshed(db_path)

    response = client.post("/admin/model", data={"csrf_token": "csrf1", "model": "brand-new-9"})

    assert response.status_code == 303
    assert _with_conn(db_path, lambda conn: choose_model(conn).saved) == "brand-new-9"
    page = client.get("/admin/?tab=settings").text
    assert '<option value="brand-new-9" selected>' in _model_row(page)
    assert not re.search(r'<span>Global settings</span><span class="toc-count"', page)


def test_state_modifiers_restyle_the_sentence_marks_too():
    """.st-line sets a default square; every state modifier must come after it to win."""
    from pathlib import Path

    import beehive.web

    css = (Path(beehive.web.__file__).parent / "static" / "admin.css").read_text()
    base = css.index(".st-line::before{")
    for modifier in (".st-paused::before", ".st-error::before", ".st-never::before", ".st-busy::before"):
        assert css.index(modifier) > base, modifier


def test_the_note_names_the_model_actually_used_when_the_default_is_gone(client, db_path):
    _with_conn(db_path, lambda conn: save_model(conn, "gpt-5.6-sol"))
    _refreshed(db_path, [ListedModel("auto", "Auto"), ListedModel("brand-new-9", "Brand New 9")])

    row = _model_row(client.get("/admin/?tab=settings").text)

    assert "gpt-5.6-sol is no longer offered, so AI work uses the default, Auto," in row
    assert '<option value="auto" selected>' in row
