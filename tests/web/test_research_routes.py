"""Route tests for the owner-only Research workspace (ADR-0008): auth on every GET/POST, CSRF on
every mutation, nav visibility, atomic create (with rollback on invalid sources), strict seed
validation, no web-side AI/network call, refresh/cancel, archive restrictions, hard delete
cascade, evidence exclude/restore (incl. empty-evidence state), one-pending-chat enforcement,
first-chat-disabled-until-synthesis, latest synthesis staying visible during a refresh, and the
exact HTMX poll/termination contract."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from beehive.auth.tokens import sign_session_id
from beehive.db.connection import connect, init_schema
from beehive.db.evidence_items import upsert_evidence_item
from beehive.db.evidence_state import create_evidence_state_revision
from beehive.db.research_chat_requests import claim_chat_request, fail_chat_request
from beehive.db.research_runs import claim_research_run, enqueue_research_run
from beehive.db.research_snapshots import add_snapshot_items, create_snapshot, seal_snapshot
from beehive.db.research_sessions import archive_research_session, create_research_session
from beehive.db.research_sources import create_research_source
from beehive.db.research_syntheses import create_synthesis
from beehive.db.sessions import create_session
from beehive.domain.research import (ClaimProvenance, EvidenceCitation, EvidenceQuality,
                                      ResearchSourceOrigin, SufficiencyState, SynthesisClaim,
                                      SynthesisSection)
from beehive.web.app import create_app
from beehive.web.deps import SESSION_COOKIE_NAME
from scripts.set_admin_password import set_admin_password

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "test.db")
    c = connect(path)
    init_schema(c)
    return path, c


@pytest.fixture
def db_path(conn):
    path, _ = conn
    return path


@pytest.fixture
def client(db_path):
    return TestClient(create_app(db_path), follow_redirects=False)


@pytest.fixture
def authed_client(db_path, conn):
    _, c = conn
    set_admin_password(db_path, "correct-password")
    create_session(c, "sess1", "csrf1", "2099-01-01T00:00:00")
    client = TestClient(create_app(db_path, session_secret="test-secret"),
                         follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, sign_session_id("sess1", "test-secret"))
    return client


def _create_session_with_evidence(c, *, with_synthesis=True):
    """Full scenario: an active session with one sealed snapshot, one evidence item, and
    (optionally) a Research Synthesis -- everything needed to submit a chat message."""
    session_id = create_research_session(c, "What is happening with rates?", T0).id
    source_id = create_research_source(
        c, session_id, "rbnz_news", {}, ResearchSourceOrigin.OWNER, T0).id
    run_id = enqueue_research_run(c, session_id, T0).id
    item = upsert_evidence_item(
        c, session_id, source_id, "e1", "Rates held", "https://example.com/1",
        EvidenceQuality.PRIMARY, T0, snippet="A snippet.")
    snapshot_id = create_snapshot(c, session_id, run_id, T0).id
    add_snapshot_items(c, snapshot_id, [item.id], T0)
    seal_snapshot(c, snapshot_id, T0)
    revision = create_evidence_state_revision(c, session_id, snapshot_id, [item.id], T0)
    if with_synthesis:
        claim = SynthesisClaim(
            text="Rates held steady", section=SynthesisSection.BOTTOM_LINE,
            provenance=ClaimProvenance.EVIDENCE,
            citations=(EvidenceCitation(
                evidence_item_id=item.id, citation_number=item.citation_number),))
        create_synthesis(
            c, session_id, revision.id, SufficiencyState.PARTIAL, (claim,), "gpt-5", "en", T0)
    return session_id, source_id, run_id, snapshot_id, item.id


def _create_session_with_two_evidence_items_and_synthesis(c):
    """Two-item variant of _create_session_with_evidence: excluding ONE item leaves the
    session's evidence non-empty (so 'disabled_no_evidence' never fires), which is what lets a
    test isolate the DISTINCT 'synthesis is stale relative to the latest revision' disabled
    state that curation always opens up (exclude/restore rebuilds the Evidence State Revision
    immediately, before any new Research Synthesis exists for it)."""
    session_id = create_research_session(c, "What is happening with rates?", T0).id
    source_id = create_research_source(
        c, session_id, "rbnz_news", {}, ResearchSourceOrigin.OWNER, T0).id
    run_id = enqueue_research_run(c, session_id, T0).id
    item1 = upsert_evidence_item(
        c, session_id, source_id, "e1", "Rates held", "https://example.com/1",
        EvidenceQuality.PRIMARY, T0, snippet="A snippet.")
    item2 = upsert_evidence_item(
        c, session_id, source_id, "e2", "Rates rising", "https://example.com/2",
        EvidenceQuality.PRIMARY, T0, snippet="Another snippet.")
    snapshot_id = create_snapshot(c, session_id, run_id, T0).id
    add_snapshot_items(c, snapshot_id, [item1.id, item2.id], T0)
    seal_snapshot(c, snapshot_id, T0)
    revision = create_evidence_state_revision(
        c, session_id, snapshot_id, [item1.id, item2.id], T0)
    claim = SynthesisClaim(
        text="Rates held steady", section=SynthesisSection.BOTTOM_LINE,
        provenance=ClaimProvenance.EVIDENCE,
        citations=(EvidenceCitation(
            evidence_item_id=item1.id, citation_number=item1.citation_number),))
    create_synthesis(
        c, session_id, revision.id, SufficiencyState.PARTIAL, (claim,), "gpt-5", "en", T0)
    c.execute("UPDATE research_runs SET status='completed', completed_at=? WHERE id=?",
              (T0.isoformat(), run_id))
    c.commit()
    return session_id, item1.id, item2.id


def _mark_run_completed(c, run_id: int, *, minutes_after_t0: int = 5) -> None:
    c.execute(
        "UPDATE research_runs SET status = 'completed', completed_at = ? WHERE id = ?",
        ((T0 + timedelta(minutes=minutes_after_t0)).isoformat(), run_id),
    )
    c.commit()


_ALL_GET_ROUTES = [
    "/research",
    "/research/new",
]
_SESSION_GET_ROUTE_TEMPLATES = [
    "/research/{id}",
    "/research/{id}/status",
    "/research/{id}/messages/status",
    "/research/{id}/refresh-preview",
    "/research/{id}/sources/new",
]
_SESSION_POST_ROUTE_TEMPLATES = [
    "/research/{id}/refresh",
    "/research/{id}/retry-synthesis",
    "/research/{id}/cancel",
    "/research/{id}/archive",
    "/research/{id}/unarchive",
    "/research/{id}/delete",
    "/research/{id}/messages",
]


# ============================================================================
# Auth: every GET and POST requires an owner session
# ============================================================================

def test_every_static_get_route_redirects_anonymous_to_login(client):
    for path in _ALL_GET_ROUTES:
        resp = client.get(path)
        assert resp.status_code == 303, path
        assert resp.headers["location"].startswith("/admin/login?next=")


def test_every_session_get_route_redirects_anonymous_to_login(client, conn):
    _, c = conn
    session_id, *_ = _create_session_with_evidence(c)
    for template in _SESSION_GET_ROUTE_TEMPLATES:
        resp = client.get(template.format(id=session_id))
        assert resp.status_code == 303, template
        assert resp.headers["location"].startswith("/admin/login?next=")


def test_every_session_post_route_redirects_anonymous_to_login(client, conn):
    _, c = conn
    session_id, *_ = _create_session_with_evidence(c)
    for template in _SESSION_POST_ROUTE_TEMPLATES:
        resp = client.post(template.format(id=session_id), data={"csrf_token": "whatever"})
        assert resp.status_code == 303, template
        assert resp.headers["location"].startswith("/admin/login?next=")


def test_evidence_exclude_restore_require_auth(client, conn):
    _, c = conn
    session_id, _, _, _, item_id = _create_session_with_evidence(c)
    for action in ("exclude", "restore"):
        resp = client.post(
            f"/research/{session_id}/evidence/{item_id}/{action}",
            data={"csrf_token": "whatever"})
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/admin/login?next=")


def test_new_session_post_requires_auth(client):
    resp = client.post("/research/new", data={"question": "Q", "csrf_token": "x"})
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/login?next=")

    preview = client.post(
        "/research/new/preview",
        data={"question": "Q", "csrf_token": "x"},
    )
    assert preview.status_code == 303
    assert preview.headers["location"].startswith("/admin/login?next=")


# ============================================================================
# CSRF: every mutation verifies the token before any DB write
# ============================================================================

def test_refresh_rejects_wrong_csrf_token(authed_client, conn):
    _, c = conn
    session_id, *_ = _create_session_with_evidence(c)
    resp = authed_client.post(
        f"/research/{session_id}/refresh", data={"csrf_token": "wrong"})
    assert resp.status_code == 403


def test_archive_rejects_wrong_csrf_token(authed_client, conn):
    _, c = conn
    session_id, *_ = _create_session_with_evidence(c)
    resp = authed_client.post(
        f"/research/{session_id}/archive", data={"csrf_token": "wrong"})
    assert resp.status_code == 403


def test_delete_rejects_wrong_csrf_token(authed_client, conn, db_path):
    _, c = conn
    session_id, *_ = _create_session_with_evidence(c)
    resp = authed_client.post(
        f"/research/{session_id}/delete", data={"csrf_token": "wrong"})
    assert resp.status_code == 403
    conn2 = connect(db_path)
    assert conn2.execute(
        "SELECT COUNT(*) FROM research_sessions").fetchone()[0] == 1


def test_evidence_exclude_rejects_wrong_csrf_token(authed_client, conn):
    _, c = conn
    session_id, _, _, _, item_id = _create_session_with_evidence(c)
    resp = authed_client.post(
        f"/research/{session_id}/evidence/{item_id}/exclude", data={"csrf_token": "wrong"})
    assert resp.status_code == 403


def test_messages_rejects_wrong_csrf_token(authed_client, conn):
    _, c = conn
    session_id, *_ = _create_session_with_evidence(c)
    resp = authed_client.post(
        f"/research/{session_id}/messages", data={"content": "hi", "csrf_token": "wrong"})
    assert resp.status_code == 403


def test_new_session_rejects_wrong_csrf_token(authed_client):
    resp = authed_client.post("/research/new", data={
        "question": "Q", "connectors": ["rbnz_news"], "csrf_token": "wrong"})
    assert resp.status_code == 403


# ============================================================================
# Nav visibility: owner vs anonymous, and never leaked on login/404
# ============================================================================

def test_nav_shows_research_link_for_owner_on_public_page(authed_client):
    resp = authed_client.get("/")
    assert resp.status_code == 200
    assert 'href="/research"' in resp.text


def test_nav_hides_research_link_for_anonymous_on_public_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'href="/research"' not in resp.text


def test_nav_hides_research_link_on_login_page(client):
    resp = client.get("/admin/login")
    assert resp.status_code == 200
    assert 'href="/research"' not in resp.text


def test_nav_hides_research_link_on_404_page(client):
    resp = client.get("/does-not-exist")
    assert resp.status_code == 404
    assert 'href="/research"' not in resp.text


def test_nav_shows_research_link_for_owner_on_admin_page(authed_client):
    resp = authed_client.get("/admin/")
    assert resp.status_code == 200
    assert 'href="/research"' in resp.text


def test_completed_research_is_unread_until_owner_opens_detail(authed_client, conn):
    _, c = conn
    session_id, _, run_id, _, _ = _create_session_with_evidence(c)
    _mark_run_completed(c, run_id)

    listing = authed_client.get("/research")
    assert listing.status_code == 200
    assert "New result" in listing.text
    assert "1 unread Research results" in listing.text

    detail = authed_client.get(f"/research/{session_id}")
    assert detail.status_code == 200

    listing_after_view = authed_client.get("/research")
    assert "New result" not in listing_after_view.text
    assert "unread Research results" not in listing_after_view.text
    assert c.execute(
        "SELECT last_viewed_at FROM research_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()["last_viewed_at"] is not None


# ============================================================================
# Create: atomic, strict seed validation, no web-side AI/network call
# ============================================================================

def test_create_session_persists_question_and_sources_atomically(authed_client, db_path):
    resp = authed_client.post("/research/new", data={
        "question": "What is happening with NZ interest rates?",
        "keyword": "",
        "connectors": ["google_news_query", "rbnz_news"],
        "reddit_subreddit": "PersonalFinanceNZ",
        "csrf_token": "csrf1",
    })
    assert resp.status_code == 303
    assert resp.headers["location"] == "/research/1"

    c = connect(db_path)
    session_row = c.execute("SELECT * FROM research_sessions").fetchone()
    assert session_row["question"] == "What is happening with NZ interest rates?"
    assert session_row["status"] == "active"
    sources = c.execute("SELECT connector_type FROM research_sources").fetchall()
    assert {r["connector_type"] for r in sources} == {
        "google_news_query", "rbnz_news", "reddit_subreddit"}
    runs = c.execute("SELECT status FROM research_runs").fetchall()
    assert len(runs) == 1
    assert runs[0]["status"] == "pending"


def test_new_session_preview_shows_enforced_budget_without_writing(
    authed_client,
    db_path,
):
    resp = authed_client.post("/research/new/preview", data={
        "question": "What is happening with NZ interest rates?",
        "connectors": ["google_news_query", "rbnz_news"],
        "csrf_token": "csrf1",
    })

    assert resp.status_code == 200
    assert "20 minutes" in resp.text
    assert "Maximum deep fetches" in resp.text
    c = connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM research_sessions").fetchone()[0] == 0


def test_create_session_rejects_blank_question_with_no_rows_written(authed_client, db_path):
    resp = authed_client.post("/research/new", data={
        "question": "   ",
        "connectors": ["rbnz_news"],
        "csrf_token": "csrf1",
    })
    assert resp.status_code == 400
    c = connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM research_sessions").fetchone()[0] == 0


def test_create_session_rejects_no_source_selected(authed_client, db_path):
    resp = authed_client.post("/research/new", data={
        "question": "A valid question", "connectors": [], "csrf_token": "csrf1",
    })
    assert resp.status_code == 400
    c = connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM research_sessions").fetchone()[0] == 0


def test_create_session_rejects_invalid_seed_and_rolls_back_everything(authed_client, db_path):
    """An over-long subreddit is rejected by web/research.py's own bound before connector_policy
    is even consulted -- either way nothing must be written."""
    resp = authed_client.post("/research/new", data={
        "question": "A valid question",
        "connectors": ["rbnz_news"],
        "reddit_subreddit": "x" * 500,
        "csrf_token": "csrf1",
    })
    assert resp.status_code == 400
    c = connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM research_sessions").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM research_sources").fetchone()[0] == 0


def test_create_session_rejects_disallowed_connector_silently_ignored_then_no_source_error(
    authed_client, db_path,
):
    """A connector value outside the allowlist is simply not selectable -- if it were the only
    one submitted, the create must fail as "no source", never smuggle it through."""
    resp = authed_client.post("/research/new", data={
        "question": "A valid question",
        "connectors": ["some_unregistered_connector"],
        "csrf_token": "csrf1",
    })
    assert resp.status_code == 400
    c = connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM research_sessions").fetchone()[0] == 0


def test_create_session_defaults_keyword_to_question(authed_client, db_path):
    resp = authed_client.post("/research/new", data={
        "question": "New Zealand economy outlook",
        "keyword": "",
        "connectors": ["google_news_query"],
        "csrf_token": "csrf1",
    })
    assert resp.status_code == 303
    c = connect(db_path)
    row = c.execute("SELECT config FROM research_sources WHERE connector_type='google_news_query'"
                     ).fetchone()
    import json
    assert json.loads(row["config"])["query"] == "New Zealand economy outlook"


def test_create_session_never_makes_a_network_or_ai_call(authed_client, db_path, monkeypatch):
    """The web process must only ever enqueue a pending Research Run -- run_data_only_prompt
    (the one function that makes an outbound LLM/network call) must never be imported or called
    by web/research.py or web/research_view.py."""
    import beehive.web.research as research_module
    import beehive.web.research_view as research_view_module
    assert "run_data_only_prompt" not in dir(research_module)
    assert "run_data_only_prompt" not in dir(research_view_module)
    resp = authed_client.post("/research/new", data={
        "question": "Q", "connectors": ["rbnz_news"], "csrf_token": "csrf1",
    })
    assert resp.status_code == 303


# ============================================================================
# Refresh / cancel
# ============================================================================

def test_refresh_enqueues_a_new_run_for_active_session(authed_client, conn):
    _, c = conn
    session_id, *_ = _create_session_with_evidence(c)
    # Complete the existing run first so a fresh refresh is legal.
    c.execute("UPDATE research_runs SET status='completed', completed_at=? WHERE session_id=?",
              (T0.isoformat(), session_id))
    c.commit()
    resp = authed_client.post(f"/research/{session_id}/refresh", data={"csrf_token": "csrf1"})
    assert resp.status_code == 303
    runs = c.execute(
        "SELECT status FROM research_runs WHERE session_id=?", (session_id,)).fetchall()
    assert len(runs) == 2
    assert any(r["status"] == "pending" for r in runs)


def test_refresh_preview_does_not_enqueue_until_confirmed(authed_client, conn):
    _, c = conn
    session_id, _, run_id, *_ = _create_session_with_evidence(c)
    c.execute(
        "UPDATE research_runs SET status='completed', completed_at=? WHERE id=?",
        (T0.isoformat(), run_id),
    )
    c.commit()

    resp = authed_client.get(f"/research/{session_id}/refresh-preview")

    assert resp.status_code == 200
    assert "Hard run ceilings" in resp.text
    assert c.execute(
        "SELECT COUNT(*) FROM research_runs WHERE session_id=?",
        (session_id,),
    ).fetchone()[0] == 1


def test_retry_synthesis_enqueues_synthesis_only_run(authed_client, conn):
    _, c = conn
    session_id, _, run_id, *_ = _create_session_with_evidence(c)
    c.execute(
        "UPDATE research_runs SET status='completed', completed_at=? WHERE id=?",
        (T0.isoformat(), run_id),
    )
    c.commit()

    resp = authed_client.post(
        f"/research/{session_id}/retry-synthesis",
        data={"csrf_token": "csrf1"},
    )

    assert resp.status_code == 303
    row = c.execute(
        "SELECT run_kind, status FROM research_runs "
        "WHERE session_id=? ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    assert (row["run_kind"], row["status"]) == ("synthesis", "pending")


def test_current_source_can_change_without_deleting_historical_evidence(
    authed_client,
    conn,
):
    _, c = conn
    session_id, source_id, run_id, _, item_id = _create_session_with_evidence(c)
    c.execute(
        "UPDATE research_runs SET status='completed', completed_at=? WHERE id=?",
        (T0.isoformat(), run_id),
    )
    c.commit()

    add = authed_client.post(
        f"/research/{session_id}/sources/new",
        data={
            "connector_type": "google_news_query",
            "value": "mortgage rates",
            "csrf_token": "csrf1",
        },
    )
    remove = authed_client.post(
        f"/research/{session_id}/sources/{source_id}/delete",
        data={"csrf_token": "csrf1"},
    )

    assert add.status_code == 303
    assert remove.status_code == 303
    assert c.execute(
        "SELECT is_active FROM research_sources WHERE id=?",
        (source_id,),
    ).fetchone()["is_active"] == 0
    assert c.execute(
        "SELECT COUNT(*) FROM research_evidence_items WHERE id=?",
        (item_id,),
    ).fetchone()[0] == 1
    detail = authed_client.get(f"/research/{session_id}?tab=evidence")
    assert detail.status_code == 200
    assert "Rates held" in detail.text


def test_plan_sources_remain_historical_and_cannot_be_managed_as_owner_sources(
    authed_client,
    conn,
):
    _, c = conn
    session_id, owner_source_id, run_id, *_ = _create_session_with_evidence(c)
    plan_source_id = create_research_source(
        c,
        session_id,
        "google_news_query",
        {"query": "historical plan query"},
        ResearchSourceOrigin.PLAN,
        T0,
    ).id
    _mark_run_completed(c, run_id)

    detail = authed_client.get(f"/research/{session_id}?tab=plan")
    assert detail.status_code == 200
    assert f"/sources/{owner_source_id}/edit" in detail.text
    assert f"/sources/{plan_source_id}/edit" not in detail.text

    edit = authed_client.get(
        f"/research/{session_id}/sources/{plan_source_id}/edit"
    )
    remove = authed_client.post(
        f"/research/{session_id}/sources/{plan_source_id}/delete",
        data={"csrf_token": "csrf1"},
    )
    assert edit.status_code == 404
    assert remove.status_code == 404

    preview = authed_client.get(f"/research/{session_id}/refresh-preview")
    assert "<dt>Current Sources</dt><dd>1</dd>" in preview.text


def test_adding_an_existing_plan_source_promotes_it_without_duplication(
    authed_client,
    conn,
):
    _, c = conn
    session_id, _, run_id, *_ = _create_session_with_evidence(c)
    plan_source_id = create_research_source(
        c,
        session_id,
        "google_news_query",
        {"query": "mortgage rates"},
        ResearchSourceOrigin.PLAN,
        T0,
    ).id
    _mark_run_completed(c, run_id)

    response = authed_client.post(
        f"/research/{session_id}/sources/new",
        data={
            "connector_type": "google_news_query",
            "value": "mortgage rates",
            "csrf_token": "csrf1",
        },
    )

    assert response.status_code == 303
    row = c.execute(
        "SELECT origin, is_active FROM research_sources WHERE id = ?",
        (plan_source_id,),
    ).fetchone()
    assert (row["origin"], row["is_active"]) == ("owner", 1)
    assert c.execute(
        """
        SELECT COUNT(*)
        FROM research_sources
        WHERE session_id = ? AND connector_type = 'google_news_query'
        """,
        (session_id,),
    ).fetchone()[0] == 1


def test_refresh_rejects_when_a_run_is_already_active(authed_client, conn):
    _, c = conn
    session_id, *_ = _create_session_with_evidence(c)
    resp = authed_client.post(f"/research/{session_id}/refresh", data={"csrf_token": "csrf1"})
    assert resp.status_code == 303
    assert "action_error=refresh" in resp.headers["location"]
    runs = c.execute(
        "SELECT COUNT(*) FROM research_runs WHERE session_id=?", (session_id,)).fetchone()[0]
    assert runs == 1


def test_refresh_rejects_archived_session(authed_client, conn):
    _, c = conn
    session_id, _, run_id, *_ = _create_session_with_evidence(c)
    c.execute("UPDATE research_runs SET status='completed', completed_at=? WHERE id=?",
              (T0.isoformat(), run_id))
    c.commit()
    archive_research_session(c, session_id, T0)
    resp = authed_client.post(f"/research/{session_id}/refresh", data={"csrf_token": "csrf1"})
    assert resp.status_code == 303
    assert "action_error=refresh" in resp.headers["location"]


def test_cancel_requests_cancellation_of_active_run(authed_client, conn):
    _, c = conn
    session_id, _, run_id, *_ = _create_session_with_evidence(c)
    resp = authed_client.post(f"/research/{session_id}/cancel", data={"csrf_token": "csrf1"})
    assert resp.status_code == 303
    row = c.execute(
        "SELECT cancel_requested FROM research_runs WHERE id=?", (run_id,)).fetchone()
    assert row["cancel_requested"] == 1


def test_cancel_errors_safely_when_nothing_is_active(authed_client, conn):
    _, c = conn
    session_id, _, run_id, *_ = _create_session_with_evidence(c)
    c.execute("UPDATE research_runs SET status='completed', completed_at=? WHERE id=?",
              (T0.isoformat(), run_id))
    c.commit()
    resp = authed_client.post(f"/research/{session_id}/cancel", data={"csrf_token": "csrf1"})
    assert resp.status_code == 303
    assert "action_error=cancel" in resp.headers["location"]


# ============================================================================
# Archive restrictions
# ============================================================================

def test_archive_blocked_while_run_is_active(authed_client, conn):
    _, c = conn
    session_id, *_ = _create_session_with_evidence(c)
    resp = authed_client.post(f"/research/{session_id}/archive", data={"csrf_token": "csrf1"})
    assert resp.status_code == 303
    assert "action_error=archive" in resp.headers["location"]
    row = c.execute(
        "SELECT status FROM research_sessions WHERE id=?", (session_id,)).fetchone()
    assert row["status"] == "active"


def test_archive_succeeds_once_run_is_terminal_and_disables_mutations(authed_client, conn):
    _, c = conn
    session_id, _, run_id, *_ = _create_session_with_evidence(c)
    c.execute("UPDATE research_runs SET status='completed', completed_at=? WHERE id=?",
              (T0.isoformat(), run_id))
    c.commit()
    resp = authed_client.post(f"/research/{session_id}/archive", data={"csrf_token": "csrf1"})
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/research/{session_id}"

    detail = authed_client.get(f"/research/{session_id}")
    assert detail.status_code == 200
    assert "action=\"/research/%s/refresh\"" % session_id not in detail.text

    # Evidence mutation is rejected server-side even if attempted directly.
    row = c.execute("SELECT id FROM research_evidence_items WHERE session_id=?",
                     (session_id,)).fetchone()
    resp2 = authed_client.post(
        f"/research/{session_id}/evidence/{row['id']}/exclude", data={"csrf_token": "csrf1"})
    assert "action_error=evidence" in resp2.headers["location"]


def test_unarchive_restores_active_status(authed_client, conn):
    _, c = conn
    session_id, _, run_id, *_ = _create_session_with_evidence(c)
    c.execute("UPDATE research_runs SET status='completed', completed_at=? WHERE id=?",
              (T0.isoformat(), run_id))
    c.commit()
    archive_research_session(c, session_id, T0)

    resp = authed_client.post(f"/research/{session_id}/unarchive", data={"csrf_token": "csrf1"})
    assert resp.status_code == 303
    row = c.execute(
        "SELECT status FROM research_sessions WHERE id=?", (session_id,)).fetchone()
    assert row["status"] == "active"


def test_archived_session_remains_readable(authed_client, conn):
    _, c = conn
    session_id, _, run_id, *_ = _create_session_with_evidence(c)
    c.execute("UPDATE research_runs SET status='completed', completed_at=? WHERE id=?",
              (T0.isoformat(), run_id))
    c.commit()
    archive_research_session(c, session_id, T0)
    resp = authed_client.get(f"/research/{session_id}")
    assert resp.status_code == 200


# ============================================================================
# Hard delete: cascade
# ============================================================================

def test_delete_cascades_every_related_row(authed_client, conn, db_path):
    _, c = conn
    session_id, source_id, run_id, snapshot_id, item_id = _create_session_with_evidence(c)
    resp = authed_client.post(f"/research/{session_id}/delete", data={"csrf_token": "csrf1"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/research"

    c2 = connect(db_path)
    assert c2.execute("SELECT COUNT(*) FROM research_sessions").fetchone()[0] == 0
    assert c2.execute("SELECT COUNT(*) FROM research_sources").fetchone()[0] == 0
    assert c2.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 0
    assert c2.execute("SELECT COUNT(*) FROM research_evidence_items").fetchone()[0] == 0
    assert c2.execute("SELECT COUNT(*) FROM research_snapshots").fetchone()[0] == 0
    assert c2.execute("SELECT COUNT(*) FROM research_syntheses").fetchone()[0] == 0


# ============================================================================
# Evidence exclude / restore, including all-excluded state
# ============================================================================

def test_evidence_exclude_then_restore_round_trip(authed_client, conn):
    _, c = conn
    session_id, _, _, _, item_id = _create_session_with_evidence(c)
    resp = authed_client.post(
        f"/research/{session_id}/evidence/{item_id}/exclude", data={"csrf_token": "csrf1"})
    assert resp.status_code == 303
    row = c.execute(
        "SELECT is_excluded FROM research_evidence_curation WHERE evidence_item_id=?",
        (item_id,)).fetchone()
    assert row["is_excluded"] == 1

    resp2 = authed_client.post(
        f"/research/{session_id}/evidence/{item_id}/restore", data={"csrf_token": "csrf1"})
    assert resp2.status_code == 303
    row2 = c.execute(
        "SELECT is_excluded FROM research_evidence_curation WHERE evidence_item_id=?",
        (item_id,)).fetchone()
    assert row2["is_excluded"] == 0


def test_excluding_all_evidence_shows_explicit_state_and_disables_chat(authed_client, conn):
    _, c = conn
    session_id, _, _, _, item_id = _create_session_with_evidence(c)
    authed_client.post(
        f"/research/{session_id}/evidence/{item_id}/exclude", data={"csrf_token": "csrf1"})
    detail = authed_client.get(f"/research/{session_id}?tab=evidence")
    assert detail.status_code == 200
    assert 'name="content"' not in detail.text or "disabled_no_evidence" not in detail.text


# ============================================================================
# Conversation: one pending request, first-chat disabled, synthesis stays visible
# ============================================================================

def test_first_chat_disabled_before_any_synthesis_exists(authed_client, conn):
    _, c = conn
    session_id, *_ = _create_session_with_evidence(c, with_synthesis=False)
    resp = authed_client.post(
        f"/research/{session_id}/messages", data={"content": "hi", "csrf_token": "csrf1"})
    assert resp.status_code == 303
    assert "action_error=message" in resp.headers["location"]


def test_curation_makes_synthesis_stale_and_hides_the_message_form(authed_client, conn):
    """Requirement: excluding evidence always rebuilds the Evidence State Revision immediately,
    before any new Research Synthesis exists for it -- the detail page must show the chat as
    disabled with a localized explanation and never render a message form guaranteed to fail."""
    _, c = conn
    session_id, item1_id, _item2_id = _create_session_with_two_evidence_items_and_synthesis(c)

    resp = authed_client.post(
        f"/research/{session_id}/evidence/{item1_id}/exclude", data={"csrf_token": "csrf1"})
    assert resp.status_code == 303

    detail = authed_client.get(f"/research/{session_id}")
    assert detail.status_code == 200
    assert 'name="content"' not in detail.text
    assert "Conversation resumes once a new synthesis" in detail.text


def test_message_submit_rejected_while_synthesis_is_stale(authed_client, conn):
    _, c = conn
    session_id, item1_id, _item2_id = _create_session_with_two_evidence_items_and_synthesis(c)
    authed_client.post(
        f"/research/{session_id}/evidence/{item1_id}/exclude", data={"csrf_token": "csrf1"})

    resp = authed_client.post(
        f"/research/{session_id}/messages", data={"content": "hi", "csrf_token": "csrf1"})
    assert resp.status_code == 303
    assert "action_error=message" in resp.headers["location"]
    assert c.execute("SELECT COUNT(*) FROM research_messages").fetchone()[0] == 0


def test_chat_reenabled_once_a_matching_synthesis_is_generated(authed_client, conn):
    _, c = conn
    session_id, item1_id, item2_id = _create_session_with_two_evidence_items_and_synthesis(c)
    authed_client.post(
        f"/research/{session_id}/evidence/{item1_id}/exclude", data={"csrf_token": "csrf1"})
    new_revision_id = c.execute(
        "SELECT id FROM research_evidence_state_revisions WHERE session_id=? "
        "ORDER BY version DESC LIMIT 1", (session_id,)).fetchone()[0]
    item2 = c.execute(
        "SELECT citation_number FROM research_evidence_items WHERE id=?",
        (item2_id,)).fetchone()
    claim = SynthesisClaim(
        text="Updated bottom line", section=SynthesisSection.BOTTOM_LINE,
        provenance=ClaimProvenance.EVIDENCE,
        citations=(EvidenceCitation(
            evidence_item_id=item2_id, citation_number=item2["citation_number"]),))
    create_synthesis(
        c, session_id, new_revision_id, SufficiencyState.PARTIAL, (claim,), "gpt-5", "en", T0)

    detail = authed_client.get(f"/research/{session_id}")
    assert detail.status_code == 200
    assert 'name="content"' in detail.text

    resp = authed_client.post(
        f"/research/{session_id}/messages", data={"content": "What changed?", "csrf_token": "csrf1"})
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/research/{session_id}"


def test_message_submit_succeeds_once_synthesis_exists(authed_client, conn):
    _, c = conn
    session_id, _, run_id, *_ = _create_session_with_evidence(c, with_synthesis=True)
    c.execute("UPDATE research_runs SET status='completed', completed_at=? WHERE id=?",
              (T0.isoformat(), run_id))
    c.commit()
    resp = authed_client.post(
        f"/research/{session_id}/messages", data={"content": "What changed?", "csrf_token": "csrf1"})
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/research/{session_id}"
    rows = c.execute(
        "SELECT content FROM research_messages WHERE session_id=?", (session_id,)).fetchall()
    assert any(r["content"] == "What changed?" for r in rows)


def test_one_pending_chat_request_rejects_a_second_submission(authed_client, conn):
    _, c = conn
    session_id, _, run_id, *_ = _create_session_with_evidence(c, with_synthesis=True)
    c.execute("UPDATE research_runs SET status='completed', completed_at=? WHERE id=?",
              (T0.isoformat(), run_id))
    c.commit()
    first = authed_client.post(
        f"/research/{session_id}/messages", data={"content": "First?", "csrf_token": "csrf1"})
    assert first.status_code == 303
    assert first.headers["location"] == f"/research/{session_id}"

    second = authed_client.post(
        f"/research/{session_id}/messages", data={"content": "Second?", "csrf_token": "csrf1"})
    assert second.status_code == 303
    assert "action_error=message" in second.headers["location"]


def test_latest_synthesis_stays_visible_while_a_refresh_runs(authed_client, conn):
    _, c = conn
    session_id, _, run_id, *_ = _create_session_with_evidence(c, with_synthesis=True)
    # The run backing the synthesis is still processing (a refresh in flight).
    claim_research_run(c, run_id, T0, lease_seconds=600, deadline_seconds=1200)
    detail = authed_client.get(f"/research/{session_id}?tab=synthesis")
    assert detail.status_code == 200
    assert "Rates held steady" in detail.text


def test_detail_page_forms_use_the_current_session_id(authed_client, conn):
    _, c = conn
    session_id, *_ = _create_session_with_evidence(c, with_synthesis=True)

    detail = authed_client.get(f"/research/{session_id}")

    assert f'action="/research/{session_id}/cancel"' in detail.text
    assert f'action="/research/{session_id}/messages"' in detail.text
    assert 'action="/research//' not in detail.text


# ============================================================================
# HTMX poll / termination contract
# ============================================================================

def test_status_partial_polls_while_run_is_pending(authed_client, conn):
    _, c = conn
    session_id, *_ = _create_session_with_evidence(c)
    resp = authed_client.get(f"/research/{session_id}/status")
    assert resp.status_code == 200
    assert 'hx-trigger="every 3s"' in resp.text
    assert 'id="research-status"' not in resp.text
    assert 'id="research-status-content"' in resp.text
    assert 'action="/research/' not in resp.text


def test_status_partial_omits_polling_once_terminal(authed_client, conn):
    _, c = conn
    session_id, _, run_id, *_ = _create_session_with_evidence(c)
    c.execute("UPDATE research_runs SET status='completed', completed_at=? WHERE id=?",
              (T0.isoformat(), run_id))
    c.commit()
    resp = authed_client.get(f"/research/{session_id}/status")
    assert resp.status_code == 200
    assert "hx-trigger" not in resp.text
    assert 'hx-swap-oob="outerHTML"' in resp.text
    assert resp.headers["HX-Refresh"] == "true"


def test_messages_status_partial_polls_only_while_chat_is_pending(authed_client, conn):
    _, c = conn
    session_id, _, run_id, *_ = _create_session_with_evidence(c, with_synthesis=True)
    c.execute("UPDATE research_runs SET status='completed', completed_at=? WHERE id=?",
              (T0.isoformat(), run_id))
    c.commit()

    idle = authed_client.get(f"/research/{session_id}/messages/status")
    assert "hx-trigger" not in idle.text

    authed_client.post(
        f"/research/{session_id}/messages", data={"content": "Hi?", "csrf_token": "csrf1"})
    pending = authed_client.get(f"/research/{session_id}/messages/status")
    assert 'hx-trigger="every 3s"' in pending.text


def test_failed_chat_request_shows_safe_retry_guidance(authed_client, conn):
    _, c = conn
    session_id, _, run_id, *_ = _create_session_with_evidence(c, with_synthesis=True)
    c.execute("UPDATE research_runs SET status='completed', completed_at=? WHERE id=?",
              (T0.isoformat(), run_id))
    c.commit()
    authed_client.post(
        f"/research/{session_id}/messages",
        data={"content": "What changed?", "csrf_token": "csrf1"})
    request_id = c.execute(
        "SELECT id FROM research_chat_requests WHERE session_id = ?", (session_id,)).fetchone()[0]
    claimed = claim_chat_request(c, request_id, T0, lease_seconds=60)
    fail_chat_request(c, request_id, claimed.claim_token, "llm", "private details", T0)

    response = authed_client.get(f"/research/{session_id}/messages/status")

    assert response.status_code == 200
    assert "This reply could not be generated." in response.text
    assert "private details" not in response.text
    assert 'action="/research/%s/messages"' % session_id in response.text


def test_detail_page_ships_the_vendored_htmx_script(authed_client, conn):
    _, c = conn
    session_id, *_ = _create_session_with_evidence(c)
    resp = authed_client.get(f"/research/{session_id}")
    assert "/static/htmx.min.js" in resp.text


# ============================================================================
# Safety: no full text / raw JSON / raw errors / claim / memory leakage
# ============================================================================

def test_detail_page_never_leaks_full_text_raw_json_or_claim_token(authed_client, conn):
    _, c = conn
    session_id, source_id, run_id, snapshot_id, item_id = _create_session_with_evidence(c)
    c.execute(
        "UPDATE research_evidence_items SET full_text = ? WHERE id = ?",
        ("SECRET FULL ARTICLE TEXT never render me", item_id))
    c.commit()
    claim = claim_research_run(c, run_id, T0, lease_seconds=600, deadline_seconds=1200)
    c.commit()
    resp = authed_client.get(f"/research/{session_id}?tab=evidence")
    assert "SECRET FULL ARTICLE TEXT" not in resp.text
    assert claim.run.claim_token not in resp.text
    assert '"connector_type"' not in resp.text  # no raw plan_json ever rendered raw


def test_error_pages_never_render_raw_exception_text(authed_client, conn):
    _, c = conn
    session_id, *_ = _create_session_with_evidence(c)
    resp = authed_client.post(f"/research/{session_id}/refresh", data={"csrf_token": "csrf1"})
    detail = authed_client.get(resp.headers["location"])
    assert "Traceback" not in detail.text
    assert "ValueError" not in detail.text


def test_unknown_session_returns_404(authed_client):
    resp = authed_client.get("/research/999999")
    assert resp.status_code == 404
