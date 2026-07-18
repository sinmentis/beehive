# tests/research/test_end_to_end.py
"""Final Research end-to-end validation: one continuous Owner story driven entirely through the
package's own public Interfaces and injected seams (connector_resolver, ArticleFetcher,
run_data_only_prompt) -- never real network I/O, a real tool-capable AI session, or a real
connector's `fetch()`. Every module exercised here (orchestrator, enrichment, synthesis,
conversation, evidence_state/evidence_curation, research_sessions) already has its own thorough
unit-test suite; this file's only job is to prove the SEAMS between them hold together for one
realistic Research Session's whole lifecycle, which no single module's own tests can observe:

    create session + seed source + initial run
    -> claim/plan/collect/enrich (full-text) -> seal snapshot + evidence-state revision
       -> generate synthesis (evidence-only core + isolated model-knowledge), all through the
          one worker-facing `run_research_orchestration` call
    -> submit/claim/process a durable chat reply (pinned context)
    -> enqueue/claim/process a refresh run that appends evidence (stable citations, old
       synthesis untouched, new synthesis version) and again synthesizes through that same call
    -> a second chat pinned to the NEW evidence/synthesis
    -> exclude one Evidence Item -> new Evidence State Revision
    -> archive restrictions (blocked by an active run; blocked by an archived session) and
       unarchive
    -> a simulated crashed/never-completed claim, then a hard delete that cascades across
       every research_* table and fences that stale claim out (crash/claim fencing tied
       directly to the cascade, not a re-run of test_research_concurrency.py's races).

Every AI call is a scripted `run_data_only_prompt` fake, patched exactly where each module
imports it (mirrors test_orchestrator.py/test_synthesis.py/test_conversation.py); the real,
tool-capable `beehive.ai.llm_client.run_prompt` is patched for the whole test and asserted never
called, and every connector fetch is served by an in-memory fake handed through
`connector_resolver` -- `beehive.connectors.registry` is never consulted for a fetch."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from beehive.connectors.base import RawItem
from beehive.db.connection import connect, init_schema
from beehive.db.evidence_items import get_evidence_item
from beehive.db.evidence_state import get_evidence_state_revision
from beehive.db.research_chat_requests import claim_chat_request
from beehive.db.research_conversation_memory import get_conversation_memory
from beehive.db.research_messages import list_message_citations
from beehive.db.research_runs import (claim_research_run, complete_research_run,
                                       enqueue_research_run, get_research_run,
                                       heartbeat_research_run)
from beehive.db.research_sessions import (archive_research_session,
                                           create_research_session_with_sources,
                                           get_research_session, hard_delete_research_session,
                                           unarchive_research_session)
from beehive.db.research_snapshots import get_latest_snapshot, list_snapshot_item_ids
from beehive.db.research_syntheses import get_synthesis, list_syntheses
from beehive.deep_read.fetch import FetchedArticle
from beehive.domain.research import (ClaimProvenance, EvidenceSnapshotStatus, ResearchRunStatus,
                                      ResearchSessionStatus, SynthesisSection)
from beehive.localization import localizer_for
from beehive.research import synthesis as synth
from beehive.research.connector_policy import normalize_and_validate_sources
from beehive.research.conversation import process_claimed_chat_request, submit_owner_message
from beehive.research.enrichment import project_for_prompt
from beehive.research.limits import MAX_EVIDENCE_TEXT_CHARS_IN_PROMPT
from beehive.research.orchestrator import RunOutcomeStatus, run_research_orchestration
from beehive.research.synthesis import exclude_evidence_item

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)
_EN = localizer_for("en")
_QUESTION = "Why did RBNZ cut rates?"

# Every research_* table that hard_delete_research_session's cascade must leave empty. Listed
# once here rather than re-deriving from schema.sql so the final assertion is a plain, obvious
# checklist a reviewer can compare directly against schema.sql's own CREATE TABLE statements.
_ALL_RESEARCH_TABLES = (
    "research_sessions", "research_sources", "research_runs", "research_plan_revisions",
    "research_evidence_items", "research_snapshots", "research_snapshot_items",
    "research_evidence_curation", "research_evidence_state_revisions",
    "research_evidence_state_revision_items", "research_evidence_clusters",
    "research_evidence_cluster_items", "research_syntheses", "research_synthesis_citations",
    "research_messages", "research_message_citations", "research_chat_requests",
    "research_conversation_memory",
)

_PLAN_MARKER = "research planning engine"
_SUFFICIENCY_MARKER = "Evidence Sufficiency engine"
_SYNTHESIS_MARKER = "research synthesis engine"
_SYNTHESIS_CORE_MARKER = "ACTIVE EVIDENCE"

# Well over MAX_EVIDENCE_TEXT_CHARS_IN_PROMPT (1500) once extracted, well under extract.py's own
# 20_000-char cap -- long enough to prove "full text persisted uncapped, prompt projection
# bounded" without needlessly slowing the test down.
_LONG_ARTICLE_PARAGRAPH = (
    "Full article content about the policy decision and its economic context. " * 60)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


# ============================================================================
# Fakes: connector, fetcher, and every scripted AI response -- no network, no tool-capable call
# ============================================================================

class _FakeConnector:
    """Everything `collect()` ever touches for this test -- handed to `run_research_orchestration`
    via `connector_resolver`, so `beehive.connectors.registry` is never consulted to fetch."""

    def __init__(self, items):
        self._items = list(items)
        self.call_count = 0

    def validate_config(self, config):
        pass

    def fetch(self, config):
        self.call_count += 1
        return list(self._items)


class _FakeFetcher:
    """Every deep-fetch this test performs -- handed to `run_research_orchestration` via
    `fetcher`, so `beehive.deep_read.fetch.ArticleFetcher`'s real, network-capable
    implementation is never constructed."""

    def __init__(self, html_by_url):
        self._html_by_url = html_by_url
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return FetchedArticle(
            url=url, status_code=200, content_type="text/html",
            html=self._html_by_url[url], truncated=False)


def _raw_item(external_id, title, url, body="connector snippet"):
    return RawItem(external_id=external_id, title=title, url=url, body=body, created_at=T0)


def _article_html(paragraph=_LONG_ARTICLE_PARAGRAPH):
    return f"<html><body><p>{paragraph}</p></body></html>"


class _AIScript:
    """Fake tool-free `run_data_only_prompt`: dispatches to a scripted plan, sufficiency, or
    synthesis (core, then model-knowledge) response purely by inspecting the prompt's own
    distinct marker phrase (mirrors test_orchestrator.py's own `_AIScript`). `run_research_
    orchestration` now calls `research.synthesis.generate_synthesis` itself before a run may
    complete, so this same script is patched at BOTH `beehive.research.orchestrator.
    run_data_only_prompt` and `beehive.research.synthesis.run_data_only_prompt`."""

    def __init__(self, plan_response, sufficiency_response, core_response, knowledge_response):
        self._plan_response = plan_response
        self._sufficiency_response = sufficiency_response
        self._core_response = core_response
        self._knowledge_response = knowledge_response

    async def __call__(self, prompt, model=None, timeout=None):
        if _SUFFICIENCY_MARKER in prompt:
            return self._sufficiency_response
        if _SYNTHESIS_MARKER in prompt:
            return (
                self._core_response if _SYNTHESIS_CORE_MARKER in prompt
                else self._knowledge_response)
        assert _PLAN_MARKER in prompt
        return self._plan_response


def _plan_response(sources, summary="Investigate the topic."):
    return f"""Plan.\n```json\n{json.dumps({"plan_summary": summary, "sources": sources})}\n```\n"""


def _sufficiency_response(state="sufficient"):
    body = {
        "state": state, "covered_sub_questions": ["Why did the rate change?"], "gaps": [],
        "contradictions": [], "new_evidence_changed_conclusions": False,
    }
    return f"""Assessment.\n```json\n{json.dumps(body)}\n```\n"""


def _core_response(aliases):
    body = {
        section: [{"text": f"{section} claim", "citations": list(aliases)}]
        for section in synth.CORE_SECTIONS
    }
    return f"""Synthesis.\n```json\n{json.dumps(body)}\n```\n"""


def _knowledge_response(notes=("General background note.",)):
    return f"""Background.\n```json\n{json.dumps({"notes": list(notes)})}\n```\n"""


def _reply_response(aliases, notes=()):
    body = {
        "claims": [{"text": "Because of the collected evidence.", "citations": list(aliases)}],
        "supplementary_notes": [{"text": note} for note in notes],
    }
    return f"""Reply.\n```json\n{json.dumps(body)}\n```\n"""


def _memory_response(memory):
    return f"""Memory.\n```json\n{json.dumps({"memory": memory})}\n```\n"""


# ============================================================================
# Small helper that drives the real public entry point (never raw SQL) for the one piece the
# worker does not yet wire together automatically: a chat reply needs its own claimed chat
# request. Research Synthesis generation is no longer a separate step here at all --
# `run_research_orchestration` (see the lifecycle below) now attempts it itself, against the
# very Evidence State Revision that same call just sealed, before a run may complete.
# ============================================================================

async def _submit_and_process_chat(conn, session_id, content, now, aliases, memory):
    chat_request = submit_owner_message(conn, session_id, content, now)
    claimed = claim_chat_request(conn, chat_request.id, now, lease_seconds=600)
    assert claimed is not None
    mock = AsyncMock(side_effect=[_reply_response(aliases), _memory_response(memory)])
    with patch("beehive.research.conversation.run_data_only_prompt", new=mock):
        reply = await process_claimed_chat_request(conn, claimed, _EN, now)
    return claimed, reply


# ============================================================================
# The end-to-end lifecycle
# ============================================================================

@pytest.mark.asyncio
async def test_research_session_full_lifecycle_end_to_end(conn):
    with patch("beehive.ai.llm_client.run_prompt") as tool_capable_mock:
        # -- 1. Create Research Session + seed source + initial Research Run -----------------
        # Through the exact public seam the web layer uses: connector_policy validates the
        # Owner-selected source (real, but network-free schema/allowlist validation) before
        # research_sessions.py persists session+source+run atomically.
        seeded = normalize_and_validate_sources([("rbnz_news", {})])
        session_id, run1_id = create_research_session_with_sources(conn, _QUESTION, seeded, T0)
        session = get_research_session(conn, session_id)
        assert session.status is ResearchSessionStatus.ACTIVE

        # -- 2. Claim + process the initial run: plan -> collect -> enrich -> seal ----------
        source_connectors_round1 = {"rbnz_news": _FakeConnector(items=[
            _raw_item("e1", "RBNZ cuts official cash rate sharply", "https://x/1"),
            _raw_item("e2", "RBNZ cuts official cash rate quickly", "https://x/2"),
        ])}
        fetcher_round1 = _FakeFetcher({
            "https://x/1": _article_html(),
            "https://x/2": _article_html(),
        })
        ai_round1 = _AIScript(
            plan_response=_plan_response(
                [{"connector_type": "rbnz_news", "config": {}, "rationale": "primary source"}]),
            sufficiency_response=_sufficiency_response(state="sufficient"),
            core_response=_core_response(("a1", "a2")), knowledge_response=_knowledge_response())

        lease1 = claim_research_run(conn, run1_id, T0, lease_seconds=600, deadline_seconds=3600)
        assert lease1 is not None
        with patch("beehive.research.orchestrator.run_data_only_prompt", new=ai_round1), \
             patch("beehive.research.synthesis.run_data_only_prompt", new=ai_round1):
            outcome1 = await run_research_orchestration(
                conn, run1_id, lease1.run.claim_token, session_id, _QUESTION, _EN,
                now_fn=lambda: T0, fetcher=fetcher_round1,
                connector_resolver=lambda t: source_connectors_round1[t])

        assert outcome1.status == RunOutcomeStatus.SUFFICIENT
        assert source_connectors_round1["rbnz_news"].call_count == 1
        assert get_research_run(conn, run1_id).status == ResearchRunStatus.COMPLETED

        snapshot1 = get_latest_snapshot(conn, session_id)
        assert snapshot1.status is EvidenceSnapshotStatus.SEALED
        assert len(list_snapshot_item_ids(conn, snapshot1.id)) == 2

        revision1 = get_evidence_state_revision(conn, outcome1.evidence_state_revision_id)
        assert len(revision1.evidence_item_ids) == 2

        e1 = get_evidence_item(conn, revision1.evidence_item_ids[0])
        e2 = get_evidence_item(conn, revision1.evidence_item_ids[1])
        assert {e1.external_key, e2.external_key} == {"e1", "e2"}
        if e1.external_key != "e1":
            e1, e2 = e2, e1
        # Stable, session-wide citation numbers: allocated in collection order, never reused.
        assert (e1.citation_number, e2.citation_number) == (1, 2)

        # Full extracted text is persisted uncapped, but any AI prompt projection is bounded --
        # the exact distinction ADR-0010/orchestrator.py's own sufficiency call relies on.
        assert e1.full_text is not None
        assert len(e1.full_text) > MAX_EVIDENCE_TEXT_CHARS_IN_PROMPT
        assert len(project_for_prompt(e1)) == MAX_EVIDENCE_TEXT_CHARS_IN_PROMPT

        # -- 3. The first Research Synthesis (evidence-only core + isolated model-knowledge) --
        #    was already generated and persisted by `run_research_orchestration` itself, above --
        #    a newly created Research Session is chat-ready after its initial worker run alone,
        #    with no separate synthesis step required. --------------------------------------
        assert outcome1.synthesis_id is not None
        synthesis1 = get_synthesis(conn, outcome1.synthesis_id)
        assert synthesis1.version == 1

        document1 = synth.build_document(conn, synthesis1)
        core_claims = [
            claim for claim in synthesis1.claims if claim.section is not SynthesisSection.MODEL_KNOWLEDGE
        ]
        knowledge_claims = [
            claim for claim in synthesis1.claims if claim.section is SynthesisSection.MODEL_KNOWLEDGE
        ]
        assert len(knowledge_claims) == 1
        assert knowledge_claims[0].provenance is ClaimProvenance.MODEL_KNOWLEDGE
        assert knowledge_claims[0].citations == ()  # model knowledge is never citation-backed
        assert all(claim.provenance is ClaimProvenance.EVIDENCE for claim in core_claims)
        assert all(claim.citations for claim in core_claims)  # every core claim is cited
        assert len(document1.model_knowledge) == 1  # structurally separate from the six sections
        cited_item_ids = {
            citation.evidence_item_id for claim in core_claims for citation in claim.citations
        }
        assert cited_item_ids == {e1.id, e2.id}

        # -- 4. Submit + claim + process the first durable chat turn (pinned context) --------
        claimed_chat1, reply1 = await _submit_and_process_chat(
            conn, session_id, "What happened?", T0 + timedelta(seconds=1),
            aliases=("a1", "a2"), memory="Compact memory v1.")
        assert claimed_chat1.pinned_evidence_state_revision_id == revision1.id
        assert claimed_chat1.pinned_synthesis_id == synthesis1.id
        assert claimed_chat1.pinned_memory_version == 0
        assert f"[{e1.citation_number}]" in reply1.content
        assert f"[{e2.citation_number}]" in reply1.content
        reply1_citations = {c.evidence_item_id for c in list_message_citations(conn, reply1.id)}
        assert reply1_citations == {e1.id, e2.id}
        memory_after_chat1 = get_conversation_memory(conn, session_id)
        assert memory_after_chat1.version == 1

        # -- 5. Enqueue a refresh run -- archiving is rejected while it is active ------------
        refresh_run = enqueue_research_run(conn, session_id, T0 + timedelta(minutes=1))
        with pytest.raises(ValueError, match="active run"):
            archive_research_session(conn, session_id, T0 + timedelta(minutes=1))

        # -- 6. Claim + process the refresh run: re-collects e1 (unchanged), adds e3 --------
        t_refresh = T0 + timedelta(minutes=2)
        source_connectors_round2 = {"rbnz_news": _FakeConnector(items=[
            _raw_item("e1", "RBNZ cuts official cash rate sharply", "https://x/1",
                      body="refreshed snippet"),
            _raw_item("e3", "Government announces new tax policy changes", "https://x/3"),
        ])}
        fetcher_round2 = _FakeFetcher({"https://x/3": _article_html()})
        ai_round2 = _AIScript(
            plan_response=_plan_response(
                [{"connector_type": "rbnz_news", "config": {}, "rationale": "refresh"}]),
            sufficiency_response=_sufficiency_response(state="sufficient"),
            core_response=_core_response(("a1", "a2", "a3")),
            knowledge_response=_knowledge_response())

        lease2 = claim_research_run(
            conn, refresh_run.id, t_refresh, lease_seconds=600, deadline_seconds=3600)
        assert lease2 is not None
        with patch("beehive.research.orchestrator.run_data_only_prompt", new=ai_round2), \
             patch("beehive.research.synthesis.run_data_only_prompt", new=ai_round2):
            outcome2 = await run_research_orchestration(
                conn, refresh_run.id, lease2.run.claim_token, session_id, _QUESTION, _EN,
                now_fn=lambda: t_refresh, fetcher=fetcher_round2,
                connector_resolver=lambda t: source_connectors_round2[t])

        assert outcome2.status == RunOutcomeStatus.SUFFICIENT
        assert source_connectors_round2["rbnz_news"].call_count == 1

        snapshot2 = get_latest_snapshot(conn, session_id)
        assert snapshot2.id != snapshot1.id
        snapshot2_item_ids = list_snapshot_item_ids(conn, snapshot2.id)
        assert len(snapshot2_item_ids) == 3  # cumulative: e1, e2 copied forward, plus new e3

        revision2 = get_evidence_state_revision(conn, outcome2.evidence_state_revision_id)
        assert revision2.version == revision1.version + 1
        assert set(revision2.evidence_item_ids) == set(snapshot2_item_ids)

        e1_after_refresh = get_evidence_item(conn, e1.id)
        e3 = next(
            get_evidence_item(conn, item_id) for item_id in revision2.evidence_item_ids
            if get_evidence_item(conn, item_id).external_key == "e3")
        # Stable citation numbers survive a refresh: e1's is untouched, e3 gets the next one.
        assert e1_after_refresh.citation_number == e1.citation_number == 1
        assert e3.citation_number == 3
        assert e1_after_refresh.snippet == "refreshed snippet"  # re-collection refreshed content
        assert e1_after_refresh.full_text == e1.full_text  # deep-fetch text preserved, not wiped

        # -- 7. The FIRST Research Synthesis's own citations remain valid, untouched ---------
        synthesis1_reloaded = get_synthesis(conn, synthesis1.id)
        assert synthesis1_reloaded.evidence_state_revision_id == revision1.id
        reloaded_cited_ids = {
            citation.evidence_item_id
            for claim in synthesis1_reloaded.claims for citation in claim.citations
        }
        assert reloaded_cited_ids == {e1.id, e2.id}  # unaffected by the refresh

        # -- 8. The SECOND Research Synthesis version, over all three items, was likewise ----
        #    already generated and persisted by the refresh run's own `run_research_
        #    orchestration` call above -- no separate synthesis step here either. ------------
        assert outcome2.synthesis_id is not None
        synthesis2 = get_synthesis(conn, outcome2.synthesis_id)
        assert synthesis2.version == 2
        assert len(list_syntheses(conn, session_id)) == 2

        # -- 9. A second chat turn, pinned to the NEW evidence/synthesis (not the first) ----
        claimed_chat2, reply2 = await _submit_and_process_chat(
            conn, session_id, "What's new?", t_refresh + timedelta(seconds=1),
            aliases=("a1", "a2", "a3"), memory="Compact memory v2.")
        assert claimed_chat2.pinned_evidence_state_revision_id == revision2.id
        assert claimed_chat2.pinned_synthesis_id == synthesis2.id
        assert claimed_chat2.pinned_memory_version == 1  # picked up chat #1's memory bump
        assert claimed_chat2.pinned_evidence_state_revision_id != claimed_chat1.pinned_evidence_state_revision_id
        assert claimed_chat2.pinned_synthesis_id != claimed_chat1.pinned_synthesis_id

        # -- 10. Exclude one Evidence Item -> a new (third) Evidence State Revision ----------
        t_exclude = t_refresh + timedelta(minutes=1)
        revision3 = exclude_evidence_item(
            conn, session_id, e2.id, t_exclude, note="duplicate coverage of the same event")
        assert revision3.version == revision2.version + 1
        assert e2.id not in revision3.evidence_item_ids
        assert {e1.id, e3.id} == set(revision3.evidence_item_ids)
        # Excluded, never deleted -- and every earlier revision is untouched (immutable).
        assert get_evidence_item(conn, e2.id) is not None
        assert e2.id in get_evidence_state_revision(conn, revision1.id).evidence_item_ids
        assert e2.id in get_evidence_state_revision(conn, revision2.id).evidence_item_ids

        # -- 11. Archive restrictions, then unarchive -----------------------------------------
        # Every run/chat request is terminal now, so archiving succeeds.
        archived = archive_research_session(conn, session_id, t_exclude)
        assert archived.status is ResearchSessionStatus.ARCHIVED

        with pytest.raises(ValueError, match="non-active Research Session"):
            enqueue_research_run(conn, session_id, t_exclude)
        with pytest.raises(ValueError, match="non-active Research Session"):
            submit_owner_message(conn, session_id, "Are you still there?", t_exclude)

        unarchived = unarchive_research_session(conn, session_id, t_exclude)
        assert unarchived.status is ResearchSessionStatus.ACTIVE

        # -- 12. Crash/claim fencing, proven exactly where it matters: a hard delete must ----
        #    revoke an in-flight (never-completed) claim, not just cascade rows away.
        t_crash = t_exclude + timedelta(minutes=1)
        crashed_run = enqueue_research_run(conn, session_id, t_crash)
        crashed_lease = claim_research_run(
            conn, crashed_run.id, t_crash, lease_seconds=600, deadline_seconds=3600)
        assert crashed_lease is not None  # simulates a worker that claimed, then crashed

        deleted = hard_delete_research_session(conn, session_id)
        assert deleted is True

        # The stale claim can no longer act -- the row it was fenced against is simply gone,
        # never a silent success.
        assert heartbeat_research_run(
            conn, crashed_run.id, crashed_lease.run.claim_token, t_crash,
            lease_seconds=60) is False
        assert complete_research_run(
            conn, crashed_run.id, crashed_lease.run.claim_token, ResearchRunStatus.COMPLETED,
            t_crash) is False
        assert get_research_run(conn, crashed_run.id) is None
        assert get_research_session(conn, session_id) is None

        # -- 13. Full relational cascade: not one row survives in any research_* table --------
        for table in _ALL_RESEARCH_TABLES:
            count = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            assert count == 0, f"{table} still has {count} row(s) after hard delete"

    # No real, tool-capable AI session was ever created -- every AI call throughout this whole
    # lifecycle was the scripted, tool-free run_data_only_prompt fake above.
    tool_capable_mock.assert_not_called()
