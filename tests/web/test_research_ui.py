"""UI/template-level regression coverage for the Research workspace: citation href/attributes,
invalid schemes degrading to non-links, stable citations after exclusion, external-link
screen-reader text, no nested interactive controls, model-knowledge labeling, empty/archived
states, the HTMX skeleton loader (never a spinner), and every supported locale rendering the
list/new/detail pages without a missing-translation error."""
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beehive.auth.tokens import sign_session_id
from beehive.db.connection import connect, init_schema
from beehive.db.evidence_curation import set_evidence_curation
from beehive.db.evidence_items import upsert_evidence_item
from beehive.db.evidence_state import create_evidence_state_revision
from beehive.db.research_runs import claim_research_run, complete_research_run, enqueue_research_run
from beehive.db.research_snapshots import add_snapshot_items, create_snapshot, seal_snapshot
from beehive.db.research_sessions import create_research_session
from beehive.db.research_sources import create_research_source
from beehive.db.research_syntheses import create_synthesis
from beehive.db.sessions import create_session
from beehive.domain.research import (ClaimProvenance, EvidenceCitation, EvidenceQuality,
                                      ResearchRunStatus, ResearchSourceOrigin, SufficiencyState,
                                      SynthesisClaim, SynthesisSection)
from beehive.localization import SUPPORTED_LANGUAGES, save_language
from beehive.web.app import create_app
from beehive.web.deps import SESSION_COOKIE_NAME
from scripts.set_admin_password import set_admin_password

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)
_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "src" / "beehive" / "web" / "templates"
_RESEARCH_TEMPLATES = [
    p for p in _TEMPLATES_DIR.glob("*research*.html")
]


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


def _build_synthesis_scenario(c, *, item_url="https://example.com/article"):
    session_id = create_research_session(c, "What is happening with rates?", T0).id
    source_id = create_research_source(
        c, session_id, "rbnz_news", {}, ResearchSourceOrigin.OWNER, T0).id
    run_id = enqueue_research_run(c, session_id, T0).id
    item = upsert_evidence_item(
        c, session_id, source_id, "e1", "Rates held", item_url,
        EvidenceQuality.PRIMARY, T0, snippet="A snippet.")
    snapshot_id = create_snapshot(c, session_id, run_id, T0).id
    add_snapshot_items(c, snapshot_id, [item.id], T0)
    seal_snapshot(c, snapshot_id, T0)
    revision = create_evidence_state_revision(c, session_id, snapshot_id, [item.id], T0)
    claims = (
        SynthesisClaim(
            text="Rates held steady this quarter", section=SynthesisSection.BOTTOM_LINE,
            provenance=ClaimProvenance.EVIDENCE,
            citations=(EvidenceCitation(
                evidence_item_id=item.id, citation_number=item.citation_number),)),
        SynthesisClaim(
            text="Historically, central banks move slowly",
            section=SynthesisSection.MODEL_KNOWLEDGE,
            provenance=ClaimProvenance.MODEL_KNOWLEDGE, citations=()),
    )
    create_synthesis(
        c, session_id, revision.id, SufficiencyState.PARTIAL, claims, "gpt-5", "en", T0)
    return session_id, item.id, item.citation_number


# ============================================================================
# Citation href/attributes, invalid schemes, stability after exclusion
# ============================================================================

def test_synthesis_citation_link_has_safe_attributes(authed_client, conn):
    _, c = conn
    session_id, item_id, citation_number = _build_synthesis_scenario(c)
    resp = authed_client.get(f"/research/{session_id}?tab=synthesis")
    assert resp.status_code == 200
    assert 'href="https://example.com/article"' in resp.text
    assert 'target="_blank"' in resp.text
    assert 'rel="noopener noreferrer"' in resp.text
    # External-link screen-reader text accompanies every such citation link.
    match = re.search(
        r'<a class="research-citation-chip"[^>]*>(.*?)</a>', resp.text)
    assert match is not None
    assert "sr-only" in match.group(1)
    assert f"[{citation_number}]" in match.group(1)
    assert "Rates held" in match.group(1)
    assert "Primary" in match.group(1)


def test_evidence_source_link_has_safe_attributes(authed_client, conn):
    _, c = conn
    session_id, item_id, _ = _build_synthesis_scenario(c)
    resp = authed_client.get(f"/research/{session_id}?tab=evidence")
    assert 'target="_blank"' in resp.text
    assert 'rel="noopener noreferrer"' in resp.text
    assert "sr-only" in resp.text


def test_invalid_evidence_url_scheme_degrades_to_non_link(authed_client, conn):
    _, c = conn
    session_id, item_id, _ = _build_synthesis_scenario(c, item_url="javascript:alert(1)")
    resp = authed_client.get(f"/research/{session_id}?tab=evidence")
    assert 'href="javascript:alert(1)"' not in resp.text
    assert 'href="#"' in resp.text


def test_citation_link_remains_valid_after_evidence_excluded(authed_client, conn):
    _, c = conn
    session_id, item_id, citation_number = _build_synthesis_scenario(c)
    set_evidence_curation(c, item_id, True, "", T0)
    resp = authed_client.get(f"/research/{session_id}?tab=synthesis")
    assert resp.status_code == 200
    assert 'href="https://example.com/article"' in resp.text
    assert f"[{citation_number}]" in resp.text


def test_model_knowledge_is_labeled_and_never_carries_citations(authed_client, conn):
    _, c = conn
    session_id, *_ = _build_synthesis_scenario(c)
    resp = authed_client.get(f"/research/{session_id}?tab=synthesis")
    assert "Historically, central banks move slowly" in resp.text
    idx = resp.text.index("Historically, central banks move slowly")
    # No citation chip immediately surrounding the model-knowledge note.
    window = resp.text[max(0, idx - 200):idx + 200]
    assert "research-citation-chip" not in window


# ============================================================================
# No nested interactive controls; loader is a skeleton, never a spinner
# ============================================================================

def test_research_templates_never_nest_a_button_inside_a_link():
    for path in _RESEARCH_TEMPLATES:
        content = path.read_text()
        assert not re.search(r"<a\b[^>]*>\s*<button\b", content), path.name


def test_research_templates_never_use_inline_styles():
    for path in _RESEARCH_TEMPLATES:
        assert 'style="' not in path.read_text(), path.name


def test_pending_run_uses_skeleton_not_spinner(authed_client, conn):
    _, c = conn
    session_id, *_ = _build_synthesis_scenario(c)
    resp = authed_client.get(f"/research/{session_id}/status")
    assert "research-skeleton" in resp.text
    assert "spinner" not in resp.text.lower()


def _fail_a_claimed_run(c, session_id: int, *, error_detail: str | None) -> None:
    run = enqueue_research_run(c, session_id, T0)
    lease = claim_research_run(c, run.id, T0, lease_seconds=60, deadline_seconds=1200)
    complete_research_run(
        c, run.id, lease.run.claim_token, ResearchRunStatus.FAILED, T0,
        error_code="synthesis_failed", error_detail=error_detail)


def test_failed_run_shows_captured_error_detail_as_technical_disclosure(authed_client, conn):
    _, c = conn
    session_id = create_research_session(c, "Q", T0).id
    _fail_a_claimed_run(
        c, session_id,
        error_detail="StructuredResponseError: no fenced ```json block found in core response")
    resp = authed_client.get(f"/research/{session_id}")
    assert resp.status_code == 200
    assert "research-error-detail" in resp.text
    assert "StructuredResponseError" in resp.text


def test_failed_run_without_captured_detail_shows_no_disclosure(authed_client, conn):
    _, c = conn
    session_id = create_research_session(c, "Q", T0).id
    _fail_a_claimed_run(c, session_id, error_detail=None)
    resp = authed_client.get(f"/research/{session_id}")
    assert resp.status_code == 200
    assert "research-error-detail" not in resp.text


# ============================================================================
# Empty / archived states
# ============================================================================

def test_evidence_empty_state_before_any_run_completes(authed_client, conn):
    _, c = conn
    session_id = create_research_session(c, "Q", T0).id
    resp = authed_client.get(f"/research/{session_id}?tab=evidence")
    assert resp.status_code == 200
    assert "empty-state" in resp.text


def test_plan_empty_state_before_any_plan_revision(authed_client, conn):
    _, c = conn
    session_id = create_research_session(c, "Q", T0).id
    create_research_source(
        c, session_id, "rbnz_news", {}, ResearchSourceOrigin.OWNER, T0)
    enqueue_research_run(c, session_id, T0)
    resp = authed_client.get(f"/research/{session_id}?tab=plan")
    assert resp.status_code == 200
    assert "empty-state" in resp.text


def test_synthesis_empty_state_before_any_synthesis(authed_client, conn):
    _, c = conn
    session_id = create_research_session(c, "Q", T0).id
    resp = authed_client.get(f"/research/{session_id}?tab=synthesis")
    assert resp.status_code == 200
    assert "empty-state" in resp.text


def test_research_list_empty_state(authed_client):
    resp = authed_client.get("/research")
    assert resp.status_code == 200
    assert "empty-state" in resp.text


# ============================================================================
# Locales: list / new / active detail / pending status / synthesis+evidence /
# conversation / archived / empty states
# ============================================================================

LANGUAGE_CODES = [language.code for language in SUPPORTED_LANGUAGES]


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_research_list_renders_in_every_supported_language(authed_client, conn, language_code):
    _, c = conn
    save_language(c, language_code)
    resp = authed_client.get("/research")
    assert resp.status_code == 200
    assert f'<html lang="{language_code}">' in resp.text
    assert "{{" not in resp.text and "}}" not in resp.text


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_research_new_form_renders_in_every_supported_language(authed_client, conn, language_code):
    _, c = conn
    save_language(c, language_code)
    resp = authed_client.get("/research/new")
    assert resp.status_code == 200
    assert f'<html lang="{language_code}">' in resp.text
    assert "{{" not in resp.text and "}}" not in resp.text


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_research_detail_active_pending_renders_in_every_supported_language(
    authed_client, conn, language_code,
):
    _, c = conn
    session_id = create_research_session(c, "What changed?", T0).id
    create_research_source(c, session_id, "rbnz_news", {}, ResearchSourceOrigin.OWNER, T0)
    enqueue_research_run(c, session_id, T0)
    save_language(c, language_code)
    resp = authed_client.get(f"/research/{session_id}")
    assert resp.status_code == 200
    assert f'<html lang="{language_code}">' in resp.text
    assert "{{" not in resp.text and "}}" not in resp.text


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_research_detail_synthesis_and_evidence_render_in_every_supported_language(
    authed_client, conn, language_code,
):
    _, c = conn
    session_id, *_ = _build_synthesis_scenario(c)
    save_language(c, language_code)
    synthesis_resp = authed_client.get(f"/research/{session_id}?tab=synthesis")
    evidence_resp = authed_client.get(f"/research/{session_id}?tab=evidence")
    assert synthesis_resp.status_code == 200
    assert evidence_resp.status_code == 200
    assert f'<html lang="{language_code}">' in synthesis_resp.text
    assert f'<html lang="{language_code}">' in evidence_resp.text
    assert "{{" not in synthesis_resp.text and "}}" not in synthesis_resp.text
    assert "{{" not in evidence_resp.text and "}}" not in evidence_resp.text


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_research_conversation_renders_in_every_supported_language(
    authed_client, conn, language_code,
):
    _, c = conn
    session_id, *_ = _build_synthesis_scenario(c)
    save_language(c, language_code)
    resp = authed_client.get(f"/research/{session_id}")
    assert resp.status_code == 200
    assert "{{" not in resp.text and "}}" not in resp.text


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_research_archived_session_renders_in_every_supported_language(
    authed_client, conn, language_code,
):
    _, c = conn
    from beehive.db.research_sessions import archive_research_session
    session_id = create_research_session(c, "Archived question", T0).id
    create_research_source(c, session_id, "rbnz_news", {}, ResearchSourceOrigin.OWNER, T0)
    run_id = enqueue_research_run(c, session_id, T0).id
    c.execute("UPDATE research_runs SET status='completed', completed_at=? WHERE id=?",
              (T0.isoformat(), run_id))
    c.commit()
    archive_research_session(c, session_id, T0)
    save_language(c, language_code)
    resp = authed_client.get(f"/research/{session_id}")
    assert resp.status_code == 200
    assert f'<html lang="{language_code}">' in resp.text
    assert "{{" not in resp.text and "}}" not in resp.text
