"""Unit tests for web/research_view.py -- the deep presentation Module backing the Research
workspace. Covers strict plan_json parsing/degradation, safe inline citation segment building
for Conversation Messages, evidence tab clustering, external link scheme safety, and that no
function here ever leaks a raw exception, full_text, or unresolved citation marker."""
from datetime import datetime, timezone

import pytest

from beehive.db.connection import connect, init_schema
from beehive.db.evidence_curation import set_evidence_curation
from beehive.db.evidence_items import upsert_evidence_item
from beehive.db.evidence_state import create_evidence_state_revision
from beehive.db.research_messages import append_message
from beehive.db.research_plan_revisions import create_plan_revision
from beehive.db.research_runs import enqueue_research_run
from beehive.db.research_snapshots import add_snapshot_items, create_snapshot, seal_snapshot
from beehive.db.research_sessions import create_research_session, get_research_session
from beehive.db.research_sources import create_research_source
from beehive.db.research_syntheses import create_synthesis
from beehive.domain.research import (ClaimProvenance, ConversationRole, EvidenceCitation,
                                      EvidenceQuality, ResearchRun, ResearchRunStatus,
                                      ResearchSourceOrigin, SufficiencyState, SynthesisClaim,
                                      SynthesisSection)
from beehive.localization import localizer_for
from beehive.research.synthesis import build_document
from beehive.web import research_view

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def t():
    return localizer_for("en")


@pytest.fixture
def scenario(conn):
    session_id = create_research_session(conn, "What is happening with rates?", T0).id
    source_id = create_research_source(
        conn, session_id, "rbnz_news", {}, ResearchSourceOrigin.OWNER, T0).id
    run_id = enqueue_research_run(conn, session_id, T0).id
    item = upsert_evidence_item(
        conn, session_id, source_id, "e1", "Rates held", "https://example.com/1",
        EvidenceQuality.PRIMARY, T0, snippet="A snippet.")
    snapshot_id = create_snapshot(conn, session_id, run_id, T0).id
    add_snapshot_items(conn, snapshot_id, [item.id], T0)
    seal_snapshot(conn, snapshot_id, T0)
    return session_id, source_id, run_id, snapshot_id, item.id, item.citation_number


# ============================================================================
# Link safety
# ============================================================================

def test_safe_external_href_allows_https():
    from beehive.web.link_safety import safe_external_href
    assert safe_external_href("https://example.com/a") == "https://example.com/a"


def test_safe_external_href_blocks_javascript_scheme():
    from beehive.web.link_safety import safe_external_href
    assert safe_external_href("javascript:alert(1)") == "#"


def test_safe_external_href_blocks_relative_and_malformed():
    from beehive.web.link_safety import safe_external_href
    assert safe_external_href("not a url") == "#"
    assert safe_external_href("/local/path") == "#"


# ============================================================================
# Plan parsing: strict, degrades safely
# ============================================================================

def test_build_plan_revision_view_parses_valid_plan(t):
    plan_json = (
        '{"plan_summary": "Search rate news", "sources": '
        '[{"connector_type": "rbnz_news", "config": {}, "rationale": "Primary source"}]}'
    )
    view = research_view.build_plan_revision_view(1, plan_json, True, 1, T0, t)
    assert view.is_available
    assert view.summary == "Search rate news"
    assert len(view.sources) == 1
    assert view.sources[0].rationale == "Primary source"


def test_build_plan_revision_view_degrades_on_invalid_json(t):
    view = research_view.build_plan_revision_view(1, "{not json", True, 1, T0, t)
    assert not view.is_available
    assert view.sources == ()


def test_build_plan_revision_view_degrades_on_missing_fields(t):
    view = research_view.build_plan_revision_view(1, '{"plan_summary": "x"}', True, 1, T0, t)
    assert not view.is_available


def test_build_plan_revision_view_degrades_on_malformed_source_entry(t):
    plan_json = '{"plan_summary": "x", "sources": [{"connector_type": "rbnz_news"}]}'
    view = research_view.build_plan_revision_view(1, plan_json, True, 1, T0, t)
    assert not view.is_available


def test_build_plan_revision_view_degrades_when_not_validated(t):
    plan_json = '{"plan_summary": "x", "sources": []}'
    view = research_view.build_plan_revision_view(1, plan_json, False, 1, T0, t)
    assert not view.is_available


def test_build_plan_views_reads_all_revisions_for_a_run(conn, scenario, t):
    _, _, run_id, _, _, _ = scenario
    create_plan_revision(conn, run_id, '{"plan_summary": "a", "sources": []}', "a", True, T0)
    create_plan_revision(conn, run_id, '{"plan_summary": "b", "sources": []}', "b", True, T0)
    views = research_view.build_plan_views(conn, run_id, t)
    assert [v.version for v in views] == [1, 2]


# ============================================================================
# Evidence tab
# ============================================================================

def test_build_evidence_tab_view_empty_before_any_sealed_snapshot(conn, t):
    session_id = create_research_session(conn, "Q", T0).id
    view = research_view.build_evidence_tab_view(conn, session_id, t)
    assert view.has_any_evidence is False
    assert view.clusters == ()
    assert view.all_excluded is False


def test_build_evidence_tab_view_lists_single_item(conn, scenario, t):
    session_id, _, _, _, item_id, citation_number = scenario
    view = research_view.build_evidence_tab_view(conn, session_id, t)
    assert view.has_any_evidence is True
    assert len(view.clusters) == 1
    cluster = view.clusters[0]
    assert cluster.is_duplicate_group is False
    assert len(cluster.items) == 1
    item_view = cluster.items[0]
    assert item_view.id == item_id
    assert item_view.citation_number == citation_number
    assert item_view.href == "https://example.com/1"
    assert item_view.is_excluded is False
    assert "full_text" not in vars(item_view)  # never exposed


def test_build_evidence_tab_view_marks_excluded_items(conn, scenario, t):
    session_id, _, _, _, item_id, _ = scenario
    set_evidence_curation(conn, item_id, True, "", T0)
    view = research_view.build_evidence_tab_view(conn, session_id, t)
    assert view.clusters[0].items[0].is_excluded is True


def test_build_evidence_tab_view_all_excluded_flag(conn, scenario, t):
    session_id, _, _, _, item_id, _ = scenario
    revision = create_evidence_state_revision(conn, session_id, scenario[3], [item_id], T0)
    assert revision.evidence_item_ids == (item_id,)
    set_evidence_curation(conn, item_id, True, "", T0)
    create_evidence_state_revision(conn, session_id, scenario[3], [], T0)
    view = research_view.build_evidence_tab_view(conn, session_id, t)
    assert view.all_excluded is True


def test_evidence_snippet_is_truncated(conn, t):
    session_id = create_research_session(conn, "Q", T0).id
    source_id = create_research_source(
        conn, session_id, "rbnz_news", {}, ResearchSourceOrigin.OWNER, T0).id
    run_id = enqueue_research_run(conn, session_id, T0).id
    long_snippet = "x" * 1000
    item = upsert_evidence_item(
        conn, session_id, source_id, "e1", "Title", "https://example.com/1",
        EvidenceQuality.REPORTING, T0, snippet=long_snippet)
    snapshot_id = create_snapshot(conn, session_id, run_id, T0).id
    add_snapshot_items(conn, snapshot_id, [item.id], T0)
    seal_snapshot(conn, snapshot_id, T0)
    view = research_view.build_evidence_tab_view(conn, session_id, t)
    rendered = view.clusters[0].items[0].snippet
    assert len(rendered) <= research_view.MAX_SNIPPET_DISPLAY_CHARS + 1
    assert rendered != long_snippet


# ============================================================================
# Synthesis tab
# ============================================================================

def _core_claim(item_id, citation_number, section, text="Claim text"):
    return SynthesisClaim(
        text=text, section=section, provenance=ClaimProvenance.EVIDENCE,
        citations=(EvidenceCitation(evidence_item_id=item_id, citation_number=citation_number),))


def test_load_synthesis_document_and_build_synthesis_tab_view(conn, scenario, t):
    session_id, _, _, snapshot_id, item_id, citation_number = scenario
    revision = create_evidence_state_revision(conn, session_id, snapshot_id, [item_id], T0)
    claims = (
        _core_claim(item_id, citation_number, SynthesisSection.BOTTOM_LINE, "Rates held steady"),
        SynthesisClaim(
            text="General background note", section=SynthesisSection.MODEL_KNOWLEDGE,
            provenance=ClaimProvenance.MODEL_KNOWLEDGE, citations=()),
    )
    create_synthesis(
        conn, session_id, revision.id, SufficiencyState.PARTIAL, claims, "gpt-5", "en", T0)

    document = research_view.load_synthesis_document(conn, session_id)
    view = research_view.build_synthesis_tab_view(document, t)
    assert view.has_synthesis is True
    assert view.version == 1
    assert len(view.bottom_line) == 1
    assert view.bottom_line[0].citations[0].citation_number == citation_number
    assert view.model_knowledge == ("General background note",)


def test_build_synthesis_tab_view_empty_state(t):
    view = research_view.build_synthesis_tab_view(None, t)
    assert view.has_synthesis is False
    assert view.bottom_line == ()


# ============================================================================
# Conversation tab: submit gating, including evidence/synthesis pin coherence
# ============================================================================

def test_build_conversation_view_allows_submit_once_synthesis_matches_latest_revision(
        conn, scenario, t):
    session_id, _, _, snapshot_id, item_id, citation_number = scenario
    revision = create_evidence_state_revision(conn, session_id, snapshot_id, [item_id], T0)
    claim = SynthesisClaim(
        text="Rates held steady", section=SynthesisSection.BOTTOM_LINE,
        provenance=ClaimProvenance.EVIDENCE,
        citations=(EvidenceCitation(item_id, citation_number),))
    document = build_document(conn, create_synthesis(
        conn, session_id, revision.id, SufficiencyState.PARTIAL, (claim,), "gpt-5", "en", T0))
    session = get_research_session(conn, session_id)

    view = research_view.build_conversation_view(conn, session, document, False, t)
    assert view.can_submit is True
    assert view.disabled_reason is None


def test_build_conversation_view_disables_submit_when_no_synthesis_exists(conn, scenario, t):
    session_id, *_ = scenario
    session = get_research_session(conn, session_id)

    view = research_view.build_conversation_view(conn, session, None, False, t)
    assert view.can_submit is False
    assert view.disabled_reason == t.text("web.research.conversation.disabled_no_synthesis")


def test_build_conversation_view_disables_submit_when_synthesis_is_stale(conn, scenario, t):
    """Requirement: chat must be disabled -- not merely allowed to fail -- while the latest
    Evidence State Revision has no matching Research Synthesis (e.g. curation just rebuilt the
    revision, and no new synthesis has been generated against it yet)."""
    session_id, _, _, snapshot_id, item_id, citation_number = scenario
    old_revision = create_evidence_state_revision(conn, session_id, snapshot_id, [item_id], T0)
    claim = SynthesisClaim(
        text="Rates held steady", section=SynthesisSection.BOTTOM_LINE,
        provenance=ClaimProvenance.EVIDENCE,
        citations=(EvidenceCitation(item_id, citation_number),))
    stale_document = build_document(conn, create_synthesis(
        conn, session_id, old_revision.id, SufficiencyState.PARTIAL, (claim,), "gpt-5", "en",
        T0))
    # A later curation rebuilds the Evidence State Revision -- no new synthesis exists for it.
    create_evidence_state_revision(conn, session_id, snapshot_id, [item_id], T0)
    session = get_research_session(conn, session_id)

    view = research_view.build_conversation_view(conn, session, stale_document, False, t)
    assert view.can_submit is False
    assert view.disabled_reason == t.text("web.research.conversation.disabled_synthesis_stale")


# ============================================================================
# Conversation citation segments -- the one place stored text is parsed
# ============================================================================

def test_build_message_view_links_known_citation_marker(conn, scenario, t):
    session_id, _, _, _, item_id, citation_number = scenario
    message = append_message(
        conn, session_id, ConversationRole.ASSISTANT,
        f"Rates held steady [{citation_number}]", T0,
    )
    # Attach the citation the way complete_chat_request_with_reply would.
    conn.execute(
        "INSERT INTO research_message_citations (message_id, evidence_item_id, citation_number) "
        "VALUES (?, ?, ?)",
        (message.id, item_id, citation_number),
    )
    conn.commit()
    view = research_view.build_message_view(conn, message, t)
    kinds = [s.kind for s in view.segments]
    assert "citation" in kinds
    citation_segment = next(s for s in view.segments if s.kind == "citation")
    assert citation_segment.href == "https://example.com/1"
    assert citation_segment.citation_number == citation_number


def test_build_message_view_renders_unknown_marker_as_plain_text(conn, scenario, t):
    session_id, _, _, _, _, _ = scenario
    # citation_number 99 was never attached to this message -- must render as plain text.
    message = append_message(
        conn, session_id, ConversationRole.ASSISTANT, "Unverified claim [99]", T0)
    view = research_view.build_message_view(conn, message, t)
    assert all(s.kind == "text" for s in view.segments)
    assert "[99]" in "".join(s.text for s in view.segments if s.kind == "text")


def test_build_message_view_never_parses_owner_message_markers(conn, scenario, t):
    session_id, _, _, _, _, _ = scenario
    message = append_message(conn, session_id, ConversationRole.OWNER, "What about [1]?", T0)
    view = research_view.build_message_view(conn, message, t)
    assert all(s.kind == "text" for s in view.segments)


# ============================================================================
# Safe error mapping
# ============================================================================

def test_safe_session_error_message_never_leaks_exception_text(t):
    exc = ValueError("some internal detail with a session_id=42 and a stack-shaped message")
    message = research_view.safe_session_error_message(t, exc)
    assert "42" not in message
    assert "session_id" not in message


def _failed_run(error_detail: str | None) -> ResearchRun:
    return ResearchRun(
        id=1, session_id=1, status=ResearchRunStatus.FAILED, phase=None, requested_at=T0,
        completed_at=T0, error_code="synthesis_failed", error_detail=error_detail)


def test_build_run_status_view_exposes_captured_error_detail_alongside_generic_message(t):
    run = _failed_run("StructuredResponseError: no fenced ```json block found in core response")
    view = research_view.build_run_status_view(run, t)
    # The friendly, localized copy is unaffected -- error_detail is a separate, additive field.
    assert view.error_message == t.text("web.research.run_error.generic")
    assert view.error_detail == (
        "StructuredResponseError: no fenced ```json block found in core response")


def test_build_run_status_view_omits_error_detail_when_none_was_captured(t):
    # Some failure paths (e.g. a lost claim) never capture a detail -- must degrade to None,
    # never a placeholder/empty string that would render an empty disclosure.
    run = _failed_run(None)
    view = research_view.build_run_status_view(run, t)
    assert view.error_message == t.text("web.research.run_error.generic")
    assert view.error_detail is None
