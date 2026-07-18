# tests/research/test_orchestrator.py
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from beehive.connectors.base import RawItem
from beehive.db.connection import connect, init_schema
from beehive.db.evidence_clusters import list_evidence_clusters
from beehive.db.evidence_curation import set_evidence_curation
from beehive.db.evidence_items import list_evidence_items_for_session, upsert_evidence_item_if_claimed
from beehive.db.evidence_state import get_evidence_state_revision, list_evidence_state_revisions
from beehive.db.research_finalization import finalize_snapshot_if_claimed
from beehive.db.research_runs import (claim_research_run, enqueue_research_run,
                                       get_research_run, heartbeat_research_run,
                                       recover_expired_research_runs, request_cancel_research_run,
                                       requeue_research_run)
from beehive.db.research_sessions import create_research_session
from beehive.db.research_snapshots import (add_snapshot_items, add_snapshot_items_if_claimed,
                                            create_snapshot, get_latest_snapshot, list_snapshots)
from beehive.db.research_sources import create_research_source
from beehive.db.research_syntheses import create_synthesis, list_syntheses
from beehive.deep_read.fetch import FetchFailure, FetchFailureReason, FetchedArticle
from beehive.domain.research import (ClaimProvenance, EvidenceCitation, EvidenceQuality,
                                      EvidenceSnapshotStatus, ResearchRunStatus,
                                      ResearchSourceOrigin, SufficiencyState, SynthesisClaim,
                                      SynthesisSection)
from beehive.localization import localizer_for
from beehive.research import synthesis as synth
from beehive.research.limits import MAX_EVIDENCE_TEXT_CHARS_IN_PROMPT
from beehive.research.orchestrator import RunOutcomeStatus, run_research_orchestration

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)
_EN = localizer_for("en")

_PLAN_MARKER = "research planning engine"
_SUFFICIENCY_MARKER = "Evidence Sufficiency engine"
_SYNTHESIS_MARKER = "research synthesis engine"
_SYNTHESIS_CORE_MARKER = "ACTIVE EVIDENCE"


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


def _claimed_run(conn, question="Why did RBNZ cut rates?", lease_seconds=600,
                  deadline_seconds=1200):
    session_id = create_research_session(conn, question, T0).id
    run = enqueue_research_run(conn, session_id, T0)
    lease = claim_research_run(
        conn, run.id, T0, lease_seconds=lease_seconds, deadline_seconds=deadline_seconds)
    return session_id, lease.run


def _plan_response(sources, summary="Investigate the topic."):
    return f"""Here is my proposed plan.

```json
{json.dumps({"plan_summary": summary, "sources": sources})}
```
"""


def _sufficiency_response(state="sufficient", gaps=None, new_evidence_changed=False):
    return f"""Here is my assessment.

```json
{json.dumps({
        "state": state,
        "covered_sub_questions": ["Why did the rate change?"],
        "gaps": gaps or [],
        "contradictions": [],
        "new_evidence_changed_conclusions": new_evidence_changed,
    })}
```
"""


def _default_synthesis_core_response():
    """A generically-valid CORE synthesis response for tests that don't care about synthesis
    content: cites only alias "a1", which is always a valid alias for any non-empty active
    Evidence State Revision (aliases are assigned fresh per call in citation_number order,
    always starting at "a1") -- safe regardless of how many Evidence Items actually exist or
    which ones curation has excluded."""
    body = {section: [{"text": f"{section} claim", "citations": ["a1"]}]
            for section in synth.CORE_SECTIONS}
    return f"""Here is the synthesis.\n```json\n{json.dumps(body)}\n```\n"""


def _default_synthesis_knowledge_response():
    return """Background.\n```json\n{"notes": ["General background note."]}\n```\n"""


class _AIScript:
    """Fake tool-free AI entry point: dispatches to a scripted plan, sufficiency, or synthesis
    response purely by inspecting the prompt text (planner.py's, sufficiency.py's, and
    synthesis.py's prompts each use distinct, stable phrasing), and records every call
    (prompt/model/timeout) for assertions. Synthesis responses default to a generically-valid
    pair (core, then model-knowledge) unless `synthesis_responses` overrides them -- most tests
    only care about evidence-gathering behavior, not synthesis content, and run_research_
    orchestration now always attempts a synthesis once any evidence exists."""

    def __init__(self, plan_responses, sufficiency_responses, synthesis_responses=None):
        self._plan_responses = list(plan_responses)
        self._sufficiency_responses = list(sufficiency_responses)
        self._synthesis_responses = (
            list(synthesis_responses) if synthesis_responses is not None else None)
        self.calls = []

    async def __call__(self, prompt, model=None, timeout=None):
        self.calls.append({"prompt": prompt, "model": model, "timeout": timeout})
        if _SUFFICIENCY_MARKER in prompt:
            return self._sufficiency_responses.pop(0)
        if _SYNTHESIS_MARKER in prompt:
            if self._synthesis_responses is not None:
                return self._synthesis_responses.pop(0)
            if _SYNTHESIS_CORE_MARKER in prompt:
                return _default_synthesis_core_response()
            return _default_synthesis_knowledge_response()
        assert _PLAN_MARKER in prompt
        return self._plan_responses.pop(0)

    @property
    def plan_calls(self):
        return [c for c in self.calls if _PLAN_MARKER in c["prompt"]]

    @property
    def sufficiency_calls(self):
        return [c for c in self.calls if _SUFFICIENCY_MARKER in c["prompt"]]

    @property
    def synthesis_calls(self):
        return [c for c in self.calls if _SYNTHESIS_MARKER in c["prompt"]]


class _FakeConnector:
    def __init__(self, items=None, raises=None):
        self._items = items or []
        self._raises = raises
        self.received_configs = []
        self.call_count = 0

    def validate_config(self, config):
        pass

    def fetch(self, config):
        self.call_count += 1
        self.received_configs.append(config)
        if self._raises is not None:
            raise self._raises
        return list(self._items)


class _FakeFetcher:
    def __init__(self, outcome_fn):
        """outcome_fn(url) -> FetchOutcome, called once per fetch."""
        self._outcome_fn = outcome_fn
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return self._outcome_fn(url)


def _raw_item(external_id, title, url, body="body"):
    return RawItem(external_id=external_id, title=title, url=url, body=body, created_at=T0)


def _complete_html(paragraph="Full article content. " * 40):
    return f"<html><body><p>{paragraph}</p></body></html>"


async def _run_orchestration(conn, session_id, run, *, connectors, ai_script, fetcher=None,
                              now_fn=None, **kwargs):
    fetcher = fetcher or _FakeFetcher(lambda url: FetchedArticle(
        url=url, status_code=200, content_type="text/html", html=_complete_html(),
        truncated=False))
    now_fn = now_fn or (lambda: T0)
    with patch("beehive.research.orchestrator.run_data_only_prompt", new=ai_script), \
         patch("beehive.research.synthesis.run_data_only_prompt", new=ai_script):
        return await run_research_orchestration(
            conn, run.id, run.claim_token, session_id, "Why did RBNZ cut rates?", _EN,
            now_fn=now_fn, fetcher=fetcher, connector_resolver=lambda t: connectors[t],
            **kwargs)


# ============================================================================
# Happy path
# ============================================================================

@pytest.mark.asyncio
async def test_happy_path_seals_evidence_and_completes_run(conn):
    session_id, run = _claimed_run(conn)
    connectors = {"rbnz_news": _FakeConnector(items=[
        _raw_item("e1", "RBNZ raises official cash rate", "https://x/1")])}
    ai_script = _AIScript(
        plan_responses=[_plan_response(
            [{"connector_type": "rbnz_news", "config": {}, "rationale": "primary source"}])],
        sufficiency_responses=[_sufficiency_response(state="sufficient")])

    outcome = await _run_orchestration(conn, session_id, run, connectors=connectors,
                                        ai_script=ai_script)

    assert outcome.status == RunOutcomeStatus.SUFFICIENT
    assert outcome.snapshot_id is not None
    assert outcome.evidence_state_revision_id is not None
    assert outcome.rounds_completed == 1
    assert outcome.sufficiency.is_sufficient

    run_row = get_research_run(conn, run.id)
    assert run_row.status == ResearchRunStatus.COMPLETED

    items = list_evidence_items_for_session(conn, session_id)
    assert len(items) == 1
    assert items[0].snippet == "body"


# ============================================================================
# Source failure isolation
# ============================================================================

@pytest.mark.asyncio
async def test_one_failing_source_does_not_abort_the_run(conn):
    session_id, run = _claimed_run(conn)
    connectors = {
        "rbnz_news": _FakeConnector(items=[
            _raw_item("g1", "RBNZ raises official cash rate", "https://x/1")]),
        "google_news_query": _FakeConnector(raises=RuntimeError("feed down")),
    }
    ai_script = _AIScript(
        plan_responses=[_plan_response([
            {"connector_type": "rbnz_news", "config": {}, "rationale": "primary"},
            {"connector_type": "google_news_query", "config": {"query": "rbnz"},
             "rationale": "aggregator"},
        ])],
        sufficiency_responses=[_sufficiency_response(state="sufficient")])

    outcome = await _run_orchestration(conn, session_id, run, connectors=connectors,
                                        ai_script=ai_script)

    assert outcome.status == RunOutcomeStatus.SUFFICIENT
    assert len(outcome.source_failures) == 1
    failure = outcome.source_failures[0]
    assert failure.connector_type == "google_news_query"
    assert failure.error_code == "RuntimeError"
    assert failure.round_number == 1
    items = list_evidence_items_for_session(conn, session_id)
    assert len(items) == 1


# ============================================================================
# Exact connector execution from the validated plan
# ============================================================================

@pytest.mark.asyncio
async def test_collector_is_called_with_exactly_the_plans_own_configs(conn):
    session_id, run = _claimed_run(conn)
    rbnz = _FakeConnector(items=[_raw_item("e1", "RBNZ headline", "https://x/1")])
    google = _FakeConnector(items=[_raw_item("e2", "Other headline", "https://x/2")])
    connectors = {"rbnz_news": rbnz, "google_news_query": google}
    ai_script = _AIScript(
        plan_responses=[_plan_response([
            {"connector_type": "rbnz_news", "config": {}, "rationale": "primary"},
            {"connector_type": "google_news_query", "config": {"query": "official cash rate"},
             "rationale": "aggregator"},
        ])],
        sufficiency_responses=[_sufficiency_response(state="sufficient")])

    await _run_orchestration(conn, session_id, run, connectors=connectors, ai_script=ai_script)

    assert rbnz.received_configs == [{}]
    assert google.received_configs == [{"query": "official cash rate"}]


# ============================================================================
# SSRF-safe fetcher reuse
# ============================================================================

@pytest.mark.asyncio
async def test_the_same_injected_fetcher_instance_is_reused_across_deep_fetches(conn):
    session_id, run = _claimed_run(conn)
    connectors = {"rbnz_news": _FakeConnector(items=[
        _raw_item("e1", "RBNZ headline one", "https://x/1"),
        _raw_item("e2", "RBNZ headline two", "https://x/2"),
    ])}
    ai_script = _AIScript(
        plan_responses=[_plan_response(
            [{"connector_type": "rbnz_news", "config": {}, "rationale": "primary"}])],
        sufficiency_responses=[_sufficiency_response(state="sufficient")])
    fetcher = _FakeFetcher(lambda url: FetchedArticle(
        url=url, status_code=200, content_type="text/html", html=_complete_html(),
        truncated=False))

    await _run_orchestration(conn, session_id, run, connectors=connectors, ai_script=ai_script,
                              fetcher=fetcher)

    assert len(fetcher.calls) == 2
    assert set(fetcher.calls) == {"https://x/1", "https://x/2"}


# ============================================================================
# Full text > 20k retained uncapped; bounded prompt projection
# ============================================================================

@pytest.mark.asyncio
async def test_full_text_over_20k_is_retained_and_prompt_projection_is_bounded(conn):
    session_id, run = _claimed_run(conn)
    connectors = {"rbnz_news": _FakeConnector(items=[
        _raw_item("e1", "RBNZ raises official cash rate", "https://x/1")])}
    long_paragraph = "This sentence discusses the rate decision in detail. " * 500
    assert len(long_paragraph) > 20_000
    fetcher = _FakeFetcher(lambda url: FetchedArticle(
        url=url, status_code=200, content_type="text/html",
        html=f"<html><body><p>{long_paragraph}</p></body></html>", truncated=False))
    ai_script = _AIScript(
        plan_responses=[_plan_response(
            [{"connector_type": "rbnz_news", "config": {}, "rationale": "primary"}])],
        sufficiency_responses=[_sufficiency_response(state="sufficient")])

    await _run_orchestration(conn, session_id, run, connectors=connectors, ai_script=ai_script,
                              fetcher=fetcher)

    items = list_evidence_items_for_session(conn, session_id)
    assert len(items[0].full_text) > 20_000

    sufficiency_prompt = ai_script.sufficiency_calls[0]["prompt"]
    text_lines = [ln for ln in sufficiency_prompt.splitlines() if ln.startswith("text: ")]
    assert text_lines
    for line in text_lines:
        assert len(line) - len("text: ") <= MAX_EVIDENCE_TEXT_CHARS_IN_PROMPT


# ============================================================================
# Partial full-text fetch retains snippet evidence
# ============================================================================

@pytest.mark.asyncio
async def test_a_failed_deep_fetch_still_retains_the_snippet(conn):
    session_id, run = _claimed_run(conn)
    connectors = {"rbnz_news": _FakeConnector(items=[
        _raw_item("e1", "RBNZ raises official cash rate", "https://x/1", body="the snippet")])}
    fetcher = _FakeFetcher(lambda url: FetchFailure(
        FetchFailureReason.PROHIBITED_ADDRESS, "blocked"))
    ai_script = _AIScript(
        plan_responses=[_plan_response(
            [{"connector_type": "rbnz_news", "config": {}, "rationale": "primary"}])],
        sufficiency_responses=[_sufficiency_response(state="sufficient")])

    outcome = await _run_orchestration(conn, session_id, run, connectors=connectors,
                                        ai_script=ai_script, fetcher=fetcher)

    assert outcome.status == RunOutcomeStatus.SUFFICIENT
    items = list_evidence_items_for_session(conn, session_id)
    assert items[0].snippet == "the snippet"
    assert items[0].full_text is None


# ============================================================================
# 30 deep-fetch reservation ceiling
# ============================================================================

@pytest.mark.asyncio
async def test_deep_fetch_ceiling_of_30_is_enforced_and_extra_candidates_keep_snippet_only(conn):
    session_id, run = _claimed_run(conn)
    rbnz_items = [_raw_item(f"r{i}", f"RBNZ headline number {i}", f"https://x/r{i}")
                  for i in range(20)]
    govt_items = [_raw_item(f"g{i}", f"Govt headline number {i}", f"https://x/g{i}")
                  for i in range(11)]
    connectors = {
        "rbnz_news": _FakeConnector(items=rbnz_items),
        "nz_government_news": _FakeConnector(items=govt_items),
    }
    fetcher = _FakeFetcher(lambda url: FetchedArticle(
        url=url, status_code=200, content_type="text/html", html=_complete_html(),
        truncated=False))
    ai_script = _AIScript(
        plan_responses=[_plan_response([
            {"connector_type": "rbnz_news", "config": {}, "rationale": "primary"},
            {"connector_type": "nz_government_news", "config": {}, "rationale": "primary too"},
        ])],
        sufficiency_responses=[_sufficiency_response(state="sufficient")])

    await _run_orchestration(conn, session_id, run, connectors=connectors, ai_script=ai_script,
                              fetcher=fetcher, max_deep_fetches_per_round=31)

    run_row = get_research_run(conn, run.id)
    assert run_row.deep_fetch_count == 30
    assert len(fetcher.calls) == 30

    items_after = list_evidence_items_for_session(conn, session_id)
    assert len(items_after) == 31
    with_full_text = [i for i in items_after if i.full_text]
    without_full_text = [i for i in items_after if not i.full_text]
    assert len(with_full_text) == 30
    assert len(without_full_text) == 1


# ============================================================================
# Deadline is passed into AI call timeouts
# ============================================================================

@pytest.mark.asyncio
async def test_remaining_deadline_is_passed_as_the_ai_call_timeout(conn):
    session_id, run = _claimed_run(conn, deadline_seconds=50)
    connectors = {"rbnz_news": _FakeConnector(items=[
        _raw_item("e1", "RBNZ headline", "https://x/1")])}
    ai_script = _AIScript(
        plan_responses=[_plan_response(
            [{"connector_type": "rbnz_news", "config": {}, "rationale": "primary"}])],
        sufficiency_responses=[_sufficiency_response(state="sufficient")])

    await _run_orchestration(conn, session_id, run, connectors=connectors, ai_script=ai_script,
                              now_fn=lambda: T0)

    assert ai_script.plan_calls[0]["timeout"] == pytest.approx(50.0)
    assert ai_script.sufficiency_calls[0]["timeout"] == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_ai_call_timeout_is_clamped_to_a_sane_minimum_near_the_deadline(conn):
    session_id, run = _claimed_run(conn, deadline_seconds=5000)
    connectors = {"rbnz_news": _FakeConnector(items=[
        _raw_item("e1", "RBNZ headline", "https://x/1")])}
    ai_script = _AIScript(
        plan_responses=[_plan_response(
            [{"connector_type": "rbnz_news", "config": {}, "rationale": "primary"}])],
        sufficiency_responses=[_sufficiency_response(state="sufficient")])

    # Almost the whole deadline has already elapsed by the time this round starts.
    clock = {"now": T0 + timedelta(seconds=4999)}
    await _run_orchestration(conn, session_id, run, connectors=connectors, ai_script=ai_script,
                              now_fn=lambda: clock["now"])

    assert ai_script.plan_calls[0]["timeout"] == pytest.approx(1.0)


# ============================================================================
# Deadline exceeded stops the run before any round runs
# ============================================================================

@pytest.mark.asyncio
async def test_deadline_already_passed_stops_before_any_round_runs(conn):
    """Task D: get_or_create_snapshot_if_claimed is the first atomic, claim- and deadline-fenced
    write this call ever attempts -- a deadline that has already arrived by the time it runs (a
    non-cancelled run, so no cancellation-finalization grace applies) is caught right there: the
    run is atomically failed outright (error_code='deadline_exceeded') and no Evidence Snapshot
    is ever created at all, reported the same way every other claim/deadline fence failure in
    this module is (STALE_CLAIM) -- never a "no evidence collected" failure implying collection
    was attempted and came up empty, since collection was never even reached."""
    session_id, run = _claimed_run(conn, deadline_seconds=100)
    connectors = {"rbnz_news": _FakeConnector(items=[
        _raw_item("e1", "RBNZ headline", "https://x/1")])}
    ai_script = _AIScript(plan_responses=[], sufficiency_responses=[])

    outcome = await _run_orchestration(
        conn, session_id, run, connectors=connectors, ai_script=ai_script,
        now_fn=lambda: T0 + timedelta(seconds=200))

    assert outcome.status == RunOutcomeStatus.STALE_CLAIM
    assert outcome.snapshot_id is None
    assert outcome.rounds_completed == 0
    assert ai_script.calls == []
    assert get_latest_snapshot(conn, session_id) is None

    run_row = get_research_run(conn, run.id)
    assert run_row.status == ResearchRunStatus.FAILED
    raw_row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert raw_row["error_code"] == "deadline_exceeded"


# ============================================================================
# Cancellation before any external call: no evidence collected
# ============================================================================

@pytest.mark.asyncio
async def test_cancellation_requested_before_the_run_starts_yields_no_evidence(conn):
    session_id, run = _claimed_run(conn)
    request_cancel_research_run(conn, run.id)
    connectors = {"rbnz_news": _FakeConnector(items=[
        _raw_item("e1", "RBNZ headline", "https://x/1")])}
    ai_script = _AIScript(plan_responses=[], sufficiency_responses=[])

    outcome = await _run_orchestration(conn, session_id, run, connectors=connectors,
                                        ai_script=ai_script)

    assert outcome.status == RunOutcomeStatus.CANCELLED_NO_EVIDENCE
    assert outcome.snapshot_id is None
    assert ai_script.calls == []
    run_row = get_research_run(conn, run.id)
    assert run_row.status == ResearchRunStatus.CANCELLED


# ============================================================================
# Cancellation after an external call: progress is preserved
# ============================================================================

@pytest.mark.asyncio
async def test_cancellation_requested_mid_run_preserves_already_collected_evidence(conn):
    session_id, run = _claimed_run(conn)

    round_one_items = [_raw_item("e1", "RBNZ raises official cash rate", "https://x/1")]

    class _CancelOnSecondFetch(_FakeConnector):
        def fetch(self, config):
            if self.call_count == 1:
                request_cancel_research_run(conn, run.id)
            return super().fetch(config)

    connector = _CancelOnSecondFetch(items=round_one_items)
    connectors = {"rbnz_news": connector}
    ai_script = _AIScript(
        plan_responses=[
            _plan_response([{"connector_type": "rbnz_news", "config": {},
                              "rationale": "primary"}]),
            _plan_response([{"connector_type": "rbnz_news", "config": {},
                              "rationale": "primary, revised"}]),
        ],
        sufficiency_responses=[_sufficiency_response(state="partial", gaps=["more needed"])])

    outcome = await _run_orchestration(conn, session_id, run, connectors=connectors,
                                        ai_script=ai_script)

    assert outcome.status == RunOutcomeStatus.CANCELLED_WITH_EVIDENCE
    assert outcome.snapshot_id is not None
    assert outcome.evidence_state_revision_id is not None
    items = list_evidence_items_for_session(conn, session_id)
    assert len(items) == 1
    run_row = get_research_run(conn, run.id)
    assert run_row.status == ResearchRunStatus.CANCELLED


# ============================================================================
# Stale claim cannot write
# ============================================================================

@pytest.mark.asyncio
async def test_a_claim_that_is_no_longer_active_makes_no_writes_at_all(conn):
    session_id, run = _claimed_run(conn)
    # Simulate another worker having reclaimed this run before this call ever runs.
    conn.execute("UPDATE research_runs SET claim_token = 'someone-elses-token' WHERE id = ?",
                 (run.id,))
    conn.commit()
    connectors = {"rbnz_news": _FakeConnector(items=[
        _raw_item("e1", "RBNZ headline", "https://x/1")])}
    ai_script = _AIScript(plan_responses=[], sufficiency_responses=[])

    outcome = await _run_orchestration(conn, session_id, run, connectors=connectors,
                                        ai_script=ai_script)

    assert outcome.status == RunOutcomeStatus.STALE_CLAIM
    assert outcome.snapshot_id is None
    assert ai_script.calls == []
    assert get_latest_snapshot(conn, session_id) is None
    assert list_evidence_items_for_session(conn, session_id) == []


@pytest.mark.asyncio
async def test_claim_lost_mid_run_stops_further_writes_but_keeps_prior_progress(conn):
    session_id, run = _claimed_run(conn)

    class _StealClaimOnSecondFetch(_FakeConnector):
        def fetch(self, config):
            if self.call_count == 1:
                conn.execute(
                    "UPDATE research_runs SET claim_token = 'stolen' WHERE id = ?", (run.id,))
                conn.commit()
            return super().fetch(config)

    connector = _StealClaimOnSecondFetch(items=[
        _raw_item("e1", "RBNZ raises official cash rate", "https://x/1")])
    connectors = {"rbnz_news": connector}
    ai_script = _AIScript(
        plan_responses=[
            _plan_response([{"connector_type": "rbnz_news", "config": {},
                              "rationale": "primary"}]),
            _plan_response([{"connector_type": "rbnz_news", "config": {},
                              "rationale": "primary, revised"}]),
        ],
        sufficiency_responses=[_sufficiency_response(state="partial", gaps=["more needed"])])

    outcome = await _run_orchestration(conn, session_id, run, connectors=connectors,
                                        ai_script=ai_script)

    assert outcome.status == RunOutcomeStatus.STALE_CLAIM
    assert outcome.snapshot_id is None
    # Round 1's evidence remains durably persisted even though the run itself never completed.
    items = list_evidence_items_for_session(conn, session_id)
    assert len(items) == 1
    # The snapshot from round 1 was never sealed -- it must never be treated as citable.
    snapshot = get_latest_snapshot(conn, session_id)
    assert snapshot is not None
    assert snapshot.status == EvidenceSnapshotStatus.BUILDING


@pytest.mark.asyncio
async def test_claim_lost_during_deep_fetch_stops_the_run_without_clobbering_fresher_evidence(
        tmp_path, conn):
    """A fake ArticleFetcher that -- from inside the network call itself -- opens a second DB
    connection, simulates another worker recovering this run's expired lease and reclaiming it
    with a brand-new claim_token, and durably writes fresher full_text evidence under that new
    claim before returning. The original (now-stale) worker's own final full-text write must be
    denied by enrichment.reserve_and_deep_fetch's claim fence, the whole orchestration run must
    stop as STALE_CLAIM, and the fresher evidence the new claim wrote must survive untouched."""
    session_id, run = _claimed_run(conn)
    connectors = {"rbnz_news": _FakeConnector(items=[
        _raw_item("e1", "RBNZ raises official cash rate", "https://x/1")])}

    def _reclaim_on_a_second_connection_then_fetch(url):
        other = connect(str(tmp_path / "test.db"))
        init_schema(other)
        other.execute(
            "UPDATE research_runs SET status = 'pending', phase = NULL, claim_token = NULL, "
            "lease_expires_at = NULL WHERE id = ?", (run.id,))
        other.commit()
        new_lease = claim_research_run(
            other, run.id, T0, lease_seconds=120, deadline_seconds=1200)
        item = list_evidence_items_for_session(other, session_id)[0]
        fresher = upsert_evidence_item_if_claimed(
            other, new_lease.run.id, new_lease.run.claim_token, session_id,
            item.research_source_id, item.external_key, item.title, item.url, item.quality, T0,
            snippet=item.snippet, full_text="fresher text from the reclaiming worker",
            raw_metadata=item.raw_metadata)
        assert fresher is not None
        other.close()
        return FetchedArticle(
            url=url, status_code=200, content_type="text/html", html=_complete_html(),
            truncated=False)

    fetcher = _FakeFetcher(_reclaim_on_a_second_connection_then_fetch)
    ai_script = _AIScript(
        plan_responses=[_plan_response(
            [{"connector_type": "rbnz_news", "config": {}, "rationale": "primary"}])],
        sufficiency_responses=[])

    outcome = await _run_orchestration(conn, session_id, run, connectors=connectors,
                                        ai_script=ai_script, fetcher=fetcher)

    assert outcome.status == RunOutcomeStatus.STALE_CLAIM
    assert outcome.snapshot_id is None
    items = list_evidence_items_for_session(conn, session_id)
    assert len(items) == 1
    # The stale worker's reserve_and_deep_fetch write was fenced out -- the reclaiming worker's
    # fresher full_text stands untouched.
    assert items[0].full_text == "fresher text from the reclaiming worker"


@pytest.mark.asyncio
async def test_deadline_heartbeat_race_during_finalization_writes_nothing_and_skips_synthesis(
        tmp_path, conn):
    """A real race between finalization (clustering/sealing/pinning) and a heartbeat on another
    connection that discovers this run's fixed deadline has already arrived: right after the
    round loop reaches sufficiency (and before this module's own atomic finalize_snapshot_if_
    claimed transaction ever starts), a second connection heartbeats this exact run at its own
    deadline_at, atomically failing it with error_code='deadline_exceeded'. Because
    finalize_snapshot_if_claimed is claim-fenced end to end, the orchestration run must detect
    the lost claim and write nothing at all -- no cluster, no sealed snapshot, no Evidence State
    Revision -- and must never attempt a Research Synthesis against evidence that was never
    actually finalized."""
    session_id, run = _claimed_run(conn, deadline_seconds=3600)
    connectors = {"rbnz_news": _FakeConnector(items=[
        _raw_item("e1", "RBNZ raises official cash rate", "https://x/1")])}

    async def _sufficiency_then_race(prompt, model=None, timeout=None):
        if _SUFFICIENCY_MARKER in prompt:
            other = connect(str(tmp_path / "test.db"))
            init_schema(other)
            ok = heartbeat_research_run(
                other, run.id, run.claim_token, run.deadline_at, lease_seconds=60)
            assert ok is False  # the deadline has "arrived" from the heartbeat's point of view
            other.close()
            return _sufficiency_response(state="sufficient")
        assert _PLAN_MARKER in prompt
        return _plan_response(
            [{"connector_type": "rbnz_news", "config": {}, "rationale": "primary source"}])

    with patch("beehive.research.orchestrator.run_data_only_prompt", new=_sufficiency_then_race), \
         patch("beehive.research.synthesis.run_data_only_prompt", new=_sufficiency_then_race):
        outcome = await run_research_orchestration(
            conn, run.id, run.claim_token, session_id, "Why did RBNZ cut rates?", _EN,
            now_fn=lambda: T0, fetcher=_FakeFetcher(lambda url: FetchedArticle(
                url=url, status_code=200, content_type="text/html", html=_complete_html(),
                truncated=False)),
            connector_resolver=lambda t: connectors[t])

    assert outcome.status == RunOutcomeStatus.STALE_CLAIM
    assert outcome.snapshot_id is None
    assert outcome.evidence_state_revision_id is None
    assert outcome.synthesis_id is None

    run_row = conn.execute(
        "SELECT status, phase, claim_token, error_code FROM research_runs WHERE id = ?",
        (run.id,)).fetchone()
    assert run_row["status"] == ResearchRunStatus.FAILED.value
    assert run_row["phase"] is None
    assert run_row["claim_token"] is None
    assert run_row["error_code"] == "deadline_exceeded"

    snapshot = get_latest_snapshot(conn, session_id)
    assert snapshot is not None
    assert snapshot.status == EvidenceSnapshotStatus.BUILDING  # never sealed
    assert list_evidence_clusters(conn, snapshot.id) == []
    assert list_evidence_state_revisions(conn, session_id) == []
    # the evidence collected before the race remains durably persisted, just never finalized
    assert len(list_evidence_items_for_session(conn, session_id)) == 1


@pytest.mark.asyncio
async def test_cancellation_observed_only_at_synthesis_admission_stops_before_any_llm_call(
        tmp_path, conn):
    """The exact race `db.research_syntheses.admit_synthesis_if_claimed` closes: an Owner's
    cancellation commits on a wholly separate connection immediately after the round loop's own
    last sufficiency call decides to stop (SUFFICIENT) -- so this call's own local
    `with_evidence_status` is already fixed to SUFFICIENT well before finalization (clustering,
    sealing, pinning) even runs, a stale signal a plain, unfenced check could easily miss. Only
    once finalization reaches `_synthesize_and_terminate`'s own atomic admission gate is the
    cancellation actually discovered, fresh, under its own lock. No synthesis LLM call may ever
    be attempted, and the run must terminalize CANCELLED_WITH_EVIDENCE (never SUFFICIENT) with
    `synthesis_id=None` -- the sealed snapshot and pinned revision remain fully valid either
    way, only the (never-started) synthesis is affected."""
    session_id, run = _claimed_run(conn, deadline_seconds=3600)
    connectors = {"rbnz_news": _FakeConnector(items=[
        _raw_item("e1", "RBNZ raises official cash rate", "https://x/1")])}
    calls = []

    async def _sufficiency_then_cancel(prompt, model=None, timeout=None):
        calls.append(prompt)
        if _SUFFICIENCY_MARKER in prompt:
            other = connect(str(tmp_path / "test.db"))
            init_schema(other)
            assert request_cancel_research_run(other, run.id) is True
            other.close()
            return _sufficiency_response(state="sufficient")
        assert _PLAN_MARKER in prompt
        return _plan_response(
            [{"connector_type": "rbnz_news", "config": {}, "rationale": "primary source"}])

    with patch("beehive.research.orchestrator.run_data_only_prompt", new=_sufficiency_then_cancel), \
         patch("beehive.research.synthesis.run_data_only_prompt", new=_sufficiency_then_cancel):
        outcome = await run_research_orchestration(
            conn, run.id, run.claim_token, session_id, "Why did RBNZ cut rates?", _EN,
            now_fn=lambda: T0, fetcher=_FakeFetcher(lambda url: FetchedArticle(
                url=url, status_code=200, content_type="text/html", html=_complete_html(),
                truncated=False)),
            connector_resolver=lambda t: connectors[t])

    # No synthesis LLM call -- neither the core call nor the model-knowledge call -- was ever
    # attempted: admission caught the cancellation before generate_synthesis could be invoked.
    assert not any(_SYNTHESIS_MARKER in prompt for prompt in calls)

    assert outcome.status == RunOutcomeStatus.CANCELLED_WITH_EVIDENCE
    assert outcome.synthesis_id is None
    assert outcome.snapshot_id is not None
    assert outcome.evidence_state_revision_id is not None

    run_row = get_research_run(conn, run.id)
    assert run_row.status == ResearchRunStatus.CANCELLED

    snapshot = get_latest_snapshot(conn, session_id)
    assert snapshot.status == EvidenceSnapshotStatus.SEALED
    assert len(list_evidence_state_revisions(conn, session_id)) == 1
    assert list_syntheses(conn, session_id) == []
    assert len(list_evidence_items_for_session(conn, session_id)) == 1


# ============================================================================
# Revision loop and novelty stop
# ============================================================================

@pytest.mark.asyncio
async def test_revision_loop_uses_gaps_and_prior_plan_to_build_the_next_round(conn):
    session_id, run = _claimed_run(conn)
    connectors = {"rbnz_news": _FakeConnector(items=[
        _raw_item("e1", "RBNZ raises official cash rate", "https://x/1")])}
    ai_script = _AIScript(
        plan_responses=[
            _plan_response([{"connector_type": "rbnz_news", "config": {},
                              "rationale": "primary"}]),
            _plan_response([{"connector_type": "rbnz_news", "config": {},
                              "rationale": "primary, round 2"}]),
        ],
        sufficiency_responses=[
            _sufficiency_response(state="partial", gaps=["need US Fed comparison"]),
            _sufficiency_response(state="sufficient"),
        ])

    outcome = await _run_orchestration(conn, session_id, run, connectors=connectors,
                                        ai_script=ai_script)

    assert outcome.status == RunOutcomeStatus.SUFFICIENT
    assert outcome.rounds_completed == 2
    # Round 2's plan prompt must be a revision prompt carrying the prior gap forward.
    second_plan_prompt = ai_script.plan_calls[1]["prompt"]
    assert "need US Fed comparison" in second_plan_prompt
    assert "REVISING" in second_plan_prompt


@pytest.mark.asyncio
async def test_novelty_exhausted_after_two_consecutive_rounds_with_no_new_evidence(conn):
    session_id, run = _claimed_run(conn)
    # The same single item is "collected" every round -- round 1 is new, rounds 2 and 3 are not.
    connectors = {"rbnz_news": _FakeConnector(items=[
        _raw_item("e1", "RBNZ raises official cash rate", "https://x/1")])}
    ai_script = _AIScript(
        plan_responses=[
            _plan_response([{"connector_type": "rbnz_news", "config": {},
                              "rationale": "primary"}]),
            _plan_response([{"connector_type": "rbnz_news", "config": {},
                              "rationale": "round 2"}]),
            _plan_response([{"connector_type": "rbnz_news", "config": {},
                              "rationale": "round 3"}]),
        ],
        sufficiency_responses=[
            _sufficiency_response(state="partial", gaps=["gap a"]),
            _sufficiency_response(state="partial", gaps=["gap a"]),
            _sufficiency_response(state="partial", gaps=["gap a"]),
        ])

    outcome = await _run_orchestration(conn, session_id, run, connectors=connectors,
                                        ai_script=ai_script)

    assert outcome.status == RunOutcomeStatus.NOVELTY_EXHAUSTED
    assert outcome.rounds_completed == 3
    items = list_evidence_items_for_session(conn, session_id)
    assert len(items) == 1


# ============================================================================
# Partial outcome: round-limit reached with evidence already collected
# ============================================================================

@pytest.mark.asyncio
async def test_round_limit_reached_still_seals_a_partial_outcome_with_evidence(conn):
    session_id, run = _claimed_run(conn)
    connectors = {"rbnz_news": _FakeConnector(items=[
        _raw_item("e1", "RBNZ raises official cash rate", "https://x/1")])}
    # Every round reports gaps and new evidence, so novelty never trips and the run only stops
    # because max_rounds is exhausted first.
    ai_script = _AIScript(
        plan_responses=[
            _plan_response([{"connector_type": "rbnz_news", "config": {},
                              "rationale": f"round {i}"}])
            for i in range(1, 3)
        ],
        sufficiency_responses=[
            _sufficiency_response(state="partial", gaps=["still missing something"],
                                   new_evidence_changed=True)
            for _ in range(2)
        ])

    outcome = await _run_orchestration(conn, session_id, run, connectors=connectors,
                                        ai_script=ai_script, max_rounds=2)

    assert outcome.status == RunOutcomeStatus.LIMITS_REACHED
    assert outcome.snapshot_id is not None
    assert outcome.evidence_state_revision_id is not None
    assert outcome.rounds_completed == 2


# ============================================================================
# Building snapshots are never exposed as citable
# ============================================================================

@pytest.mark.asyncio
async def test_zero_evidence_never_exposes_a_citable_snapshot(conn):
    session_id, run = _claimed_run(conn)
    connectors = {"rbnz_news": _FakeConnector(raises=RuntimeError("always down"))}
    ai_script = _AIScript(
        plan_responses=[_plan_response(
            [{"connector_type": "rbnz_news", "config": {}, "rationale": "primary"}])
            for _ in range(2)],
        sufficiency_responses=[_sufficiency_response(state="insufficient") for _ in range(2)])

    outcome = await _run_orchestration(conn, session_id, run, connectors=connectors,
                                        ai_script=ai_script, max_rounds=2)

    assert outcome.status == RunOutcomeStatus.FAILED_NO_EVIDENCE
    assert outcome.snapshot_id is None
    assert outcome.evidence_state_revision_id is None
    # A "building" snapshot may still exist (created up front), but it is never sealed.
    snapshot = get_latest_snapshot(conn, session_id)
    if snapshot is not None:
        assert snapshot.status == EvidenceSnapshotStatus.BUILDING
    run_row = get_research_run(conn, run.id)
    assert run_row.status == ResearchRunStatus.FAILED
    raw_row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert raw_row["error_code"] == "no_evidence_collected"


# ============================================================================
# Citation stability across rounds and re-collection
# ============================================================================

@pytest.mark.asyncio
async def test_citation_number_is_stable_across_rounds_for_the_same_source_item(conn):
    session_id, run = _claimed_run(conn)
    # Same connector_type/config and same external_id collected again in round 2.
    connectors = {"rbnz_news": _FakeConnector(items=[
        _raw_item("e1", "RBNZ raises official cash rate", "https://x/1")])}
    ai_script = _AIScript(
        plan_responses=[
            _plan_response([{"connector_type": "rbnz_news", "config": {},
                              "rationale": "primary"}]),
            _plan_response([{"connector_type": "rbnz_news", "config": {},
                              "rationale": "primary, round 2"}]),
        ],
        sufficiency_responses=[
            _sufficiency_response(state="partial", gaps=["need more"]),
            _sufficiency_response(state="sufficient"),
        ])

    await _run_orchestration(conn, session_id, run, connectors=connectors, ai_script=ai_script)

    items = list_evidence_items_for_session(conn, session_id)
    assert len(items) == 1
    assert items[0].citation_number == 1


# ============================================================================
# Research Synthesis: a run may no longer complete without one being attempted
# ============================================================================

@pytest.mark.asyncio
async def test_happy_path_also_produces_a_persisted_research_synthesis(conn):
    session_id, run = _claimed_run(conn)
    connectors = {"rbnz_news": _FakeConnector(items=[
        _raw_item("e1", "RBNZ raises official cash rate", "https://x/1")])}
    ai_script = _AIScript(
        plan_responses=[_plan_response(
            [{"connector_type": "rbnz_news", "config": {}, "rationale": "primary source"}])],
        sufficiency_responses=[_sufficiency_response(state="sufficient")])

    outcome = await _run_orchestration(conn, session_id, run, connectors=connectors,
                                        ai_script=ai_script)

    assert outcome.status == RunOutcomeStatus.SUFFICIENT
    assert outcome.synthesis_id is not None
    assert len(ai_script.synthesis_calls) == 2  # the core call, then the model-knowledge call
    run_row = get_research_run(conn, run.id)
    assert run_row.status == ResearchRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_synthesis_failure_preserves_evidence_and_fails_run_with_typed_code(conn):
    session_id, run = _claimed_run(conn)
    connectors = {"rbnz_news": _FakeConnector(items=[
        _raw_item("e1", "RBNZ raises official cash rate", "https://x/1")])}
    ai_script = _AIScript(
        plan_responses=[_plan_response(
            [{"connector_type": "rbnz_news", "config": {}, "rationale": "primary source"}])],
        sufficiency_responses=[_sufficiency_response(state="sufficient")],
        # Two identical malformed responses: the CORE call's one corrective retry (synthesis.py's
        # _call_core) does not help a response this broken (no fenced json block at all), so the
        # run still fails with a typed, captured reason after the retry is exhausted.
        synthesis_responses=["not a fenced json response at all",
                              "not a fenced json response at all"])

    outcome = await _run_orchestration(conn, session_id, run, connectors=connectors,
                                        ai_script=ai_script)

    assert outcome.status == RunOutcomeStatus.SYNTHESIS_FAILED
    assert outcome.synthesis_id is None
    # The sealed snapshot and pinned Evidence State Revision this same call already produced
    # remain fully valid and citable -- only the synthesis attempt itself failed.
    assert outcome.snapshot_id is not None
    assert outcome.evidence_state_revision_id is not None
    snapshot = get_latest_snapshot(conn, session_id)
    assert snapshot.status == EvidenceSnapshotStatus.SEALED
    items = list_evidence_items_for_session(conn, session_id)
    assert len(items) == 1

    run_row = get_research_run(conn, run.id)
    assert run_row.status == ResearchRunStatus.FAILED
    assert run_row.error_code == "synthesis_failed"
    # The real cause is no longer silently discarded -- the causing exception's own type name
    # and message are captured, matching collector/research_worker.py's own _classify_error
    # convention (see orchestrator.py's _synthesize_and_terminate).
    assert run_row.error_detail is not None
    assert "StructuredResponseError" in run_row.error_detail
    assert "fenced" in run_row.error_detail


# ============================================================================
# Evidence Curation overlay is applied whenever finalization creates a NEW revision
# ============================================================================

@pytest.mark.asyncio
async def test_refresh_run_excludes_previously_excluded_item_from_new_revision(conn):
    session_id, run1 = _claimed_run(conn)
    connectors_round1 = {"rbnz_news": _FakeConnector(items=[
        _raw_item("e1", "RBNZ raises official cash rate", "https://x/1")])}
    ai_script1 = _AIScript(
        plan_responses=[_plan_response(
            [{"connector_type": "rbnz_news", "config": {}, "rationale": "primary"}])],
        sufficiency_responses=[_sufficiency_response(state="sufficient")])

    outcome1 = await _run_orchestration(conn, session_id, run1, connectors=connectors_round1,
                                         ai_script=ai_script1)
    assert outcome1.status == RunOutcomeStatus.SUFFICIENT
    item1 = list_evidence_items_for_session(conn, session_id)[0]

    # The Owner excludes item 1 through the curation overlay directly (the same overlay
    # research.synthesis.exclude_evidence_item mutates -- exercised here in isolation from that
    # module's own immediate revision rebuild).
    set_evidence_curation(conn, item1.id, True, "no longer relevant", T0)

    # A refresh run appends item 2. The new snapshot is cumulative (item 1 copied forward, per
    # ADR-0010's evidence-survives-refresh guarantee), but the new Evidence State Revision this
    # run's finalization creates must still honor the Owner's exclusion.
    run2 = enqueue_research_run(conn, session_id, T0)
    lease2 = claim_research_run(conn, run2.id, T0, lease_seconds=600, deadline_seconds=1200)
    connectors_round2 = {"rbnz_news": _FakeConnector(items=[
        _raw_item("e2", "RBNZ signals further cuts", "https://x/2")])}
    ai_script2 = _AIScript(
        plan_responses=[_plan_response(
            [{"connector_type": "rbnz_news", "config": {}, "rationale": "refresh"}])],
        sufficiency_responses=[_sufficiency_response(state="sufficient")])

    outcome2 = await _run_orchestration(
        conn, session_id, lease2.run, connectors=connectors_round2, ai_script=ai_script2)

    assert outcome2.status == RunOutcomeStatus.SUFFICIENT
    all_items = list_evidence_items_for_session(conn, session_id)
    assert len(all_items) == 2  # excluded, never deleted -- item 1 still durably persisted
    item2 = next(i for i in all_items if i.external_key == "e2")

    revision2 = get_evidence_state_revision(conn, outcome2.evidence_state_revision_id)
    assert set(revision2.evidence_item_ids) == {item2.id}
    assert item1.id not in revision2.evidence_item_ids  # excluded item never reactivates


# ============================================================================
# Owner-selected Research Sources are always executed, regardless of the AI's plan
# ============================================================================

@pytest.mark.asyncio
async def test_owner_selected_source_is_fetched_even_when_the_plan_omits_it(conn):
    session_id, run = _claimed_run(conn)
    create_research_source(conn, session_id, "rbnz_news", {}, ResearchSourceOrigin.OWNER, T0)
    owner_connector = _FakeConnector(items=[
        _raw_item("e1", "RBNZ raises official cash rate", "https://x/1")])
    plan_connector = _FakeConnector(items=[
        _raw_item("g1", "Unrelated aggregator coverage", "https://x/2")])
    connectors = {"rbnz_news": owner_connector, "google_news_query": plan_connector}
    # The AI's plan proposes only a DIFFERENT source -- it never mentions the Owner's own
    # rbnz_news selection at all.
    ai_script = _AIScript(
        plan_responses=[_plan_response(
            [{"connector_type": "google_news_query", "config": {"query": "rbnz"},
              "rationale": "aggregator"}])],
        sufficiency_responses=[_sufficiency_response(state="sufficient")])

    await _run_orchestration(conn, session_id, run, connectors=connectors, ai_script=ai_script)

    assert owner_connector.call_count == 1  # never omitted just because the AI didn't mention it
    assert plan_connector.call_count == 1
    items = list_evidence_items_for_session(conn, session_id)
    assert {item.external_key for item in items} == {"e1", "g1"}


# ============================================================================
# Crash recovery: resuming a run's own staged 'building' snapshot before cancellation
# ============================================================================

@pytest.mark.asyncio
async def test_recovered_cancelled_run_resumes_and_seals_its_own_staged_building_snapshot(conn):
    session_id, run = _claimed_run(conn)

    # Simulate an earlier, crashed attempt of THIS SAME run: it already created a 'building'
    # snapshot and staged one Evidence Item into it before crashing.
    staged_snapshot = create_snapshot(conn, session_id, run.id, T0)
    source_id = create_research_source(
        conn, session_id, "rbnz_news", {}, ResearchSourceOrigin.OWNER, T0).id
    staged_item = upsert_evidence_item_if_claimed(
        conn, run.id, run.claim_token, session_id, source_id, "e1", "RBNZ raises rate",
        "https://x/1", EvidenceQuality.REPORTING, T0, snippet="staged before the crash")
    assert staged_item is not None
    add_snapshot_items(conn, staged_snapshot.id, [staged_item.id], T0)

    # The run is then recovered in an already-cancelled state -- e.g. the Owner requested
    # cancellation while the worker that staged the snapshot above was down.
    request_cancel_research_run(conn, run.id)

    connectors = {"rbnz_news": _FakeConnector(items=[])}
    ai_script = _AIScript(plan_responses=[], sufficiency_responses=[])

    outcome = await _run_orchestration(conn, session_id, run, connectors=connectors,
                                        ai_script=ai_script)

    # The staged snapshot is RESUMED (never a second, empty one) and sealed, so the run
    # completes CANCELLED_WITH_EVIDENCE instead of stranding it / reporting no evidence -- but
    # cancellation blocks a NEW Research Synthesis just like it blocks any other new AI-authored
    # write, regardless of how much budget remains: admit_synthesis_if_claimed's own fresh,
    # lock-held check finds cancel_requested already set and skips the two LLM calls entirely.
    assert outcome.status == RunOutcomeStatus.CANCELLED_WITH_EVIDENCE
    assert outcome.snapshot_id == staged_snapshot.id
    assert outcome.synthesis_id is None
    assert ai_script.plan_calls == []
    assert ai_script.sufficiency_calls == []
    assert ai_script.synthesis_calls == []

    sealed_snapshot = get_latest_snapshot(conn, session_id)
    assert sealed_snapshot.id == staged_snapshot.id
    assert sealed_snapshot.status == EvidenceSnapshotStatus.SEALED
    run_row = get_research_run(conn, run.id)
    assert run_row.status == ResearchRunStatus.CANCELLED


# ============================================================================
# Task A: cancellation-finalization grace across a full requeue/reclaim cycle past deadline
# ============================================================================

@pytest.mark.asyncio
async def test_expired_cancelled_run_requeues_reclaims_past_deadline_and_seals_then_cancels(conn):
    """The full Task A end-to-end story: a run is cancelled while still genuinely processing,
    its lease then expires, and its own fixed deadline_at ALSO passes before anyone reclaims it.
    recover_expired_research_runs must requeue it (never fail it outright) because it is
    cancelled; claim_research_run must then be able to reclaim that past-deadline pending run
    with a real (if short, finalize-only) lease; and orchestration on that reclaimed claim must
    go straight to sealing the evidence already staged before the crash and terminalizing
    CANCELLED -- never attempting a single plan, connector, sufficiency, or synthesis AI call,
    since the deadline has already passed."""
    session_id, run = _claimed_run(conn, lease_seconds=30, deadline_seconds=60)
    deadline_at = run.deadline_at

    # Evidence already staged by an earlier, still-live attempt of this exact run before it was
    # cancelled and its worker went away.
    staged_snapshot = create_snapshot(conn, session_id, run.id, T0)
    source_id = create_research_source(
        conn, session_id, "rbnz_news", {}, ResearchSourceOrigin.OWNER, T0).id
    staged_item = upsert_evidence_item_if_claimed(
        conn, run.id, run.claim_token, session_id, source_id, "e1", "RBNZ raises rate",
        "https://x/1", EvidenceQuality.REPORTING, T0, snippet="staged before cancellation")
    assert staged_item is not None
    add_snapshot_items(conn, staged_snapshot.id, [staged_item.id], T0)
    assert request_cancel_research_run(conn, run.id) is True

    # Both the short lease and the run's own fixed deadline have now passed, with nobody having
    # reclaimed it yet.
    past_deadline = deadline_at + timedelta(seconds=10)
    recovery = recover_expired_research_runs(conn, past_deadline)
    assert recovery.requeued_count == 1  # never deadline_exceeded_count -- cancellation grace
    assert recovery.deadline_exceeded_count == 0
    requeued = get_research_run(conn, run.id)
    assert requeued.status == ResearchRunStatus.PENDING
    assert requeued.deadline_at == deadline_at  # never reset

    reclaimed = claim_research_run(
        conn, run.id, past_deadline, lease_seconds=30, deadline_seconds=3600)
    assert reclaimed is not None  # never refused/zero-leased despite the past deadline
    assert reclaimed.run.deadline_at == deadline_at
    reclaimed_run = reclaimed.run

    connectors = {"rbnz_news": _FakeConnector(items=[
        _raw_item("e2", "Should never be fetched", "https://x/2")])}
    ai_script = _AIScript(plan_responses=[], sufficiency_responses=[])

    outcome = await _run_orchestration(
        conn, session_id, reclaimed_run, connectors=connectors, ai_script=ai_script,
        now_fn=lambda: past_deadline)

    # No new work of any kind was ever started past the deadline.
    assert ai_script.calls == []
    assert connectors["rbnz_news"].call_count == 0

    # The staged evidence is preserved: resumed (never re-created), sealed, and pinned -- but no
    # synthesis was attempted (zero remaining budget for a cancelled run past its deadline).
    assert outcome.status == RunOutcomeStatus.CANCELLED_WITH_EVIDENCE
    assert outcome.snapshot_id == staged_snapshot.id
    assert outcome.evidence_state_revision_id is not None
    assert outcome.synthesis_id is None
    assert list_syntheses(conn, session_id) == []

    sealed_snapshot = get_latest_snapshot(conn, session_id)
    assert sealed_snapshot.id == staged_snapshot.id
    assert sealed_snapshot.status == EvidenceSnapshotStatus.SEALED
    assert len(list_snapshots(conn, session_id)) == 1  # never a second, stray snapshot

    final = get_research_run(conn, run.id)
    assert final.status == ResearchRunStatus.CANCELLED
    assert final.claim_token is None


@pytest.mark.asyncio
async def test_non_cancelled_run_past_its_deadline_still_fails_without_ever_finalizing(conn):
    """Coherence check paired with the test above: an otherwise-identical run that was NEVER
    cancelled, whose lease and deadline have both passed, must still be reconciled the ordinary
    way -- failed outright by recover_expired_research_runs (never requeued), never reclaimable,
    and its staged evidence never sealed. The Task A grace is strictly gated on cancel_requested."""
    session_id, run = _claimed_run(conn, lease_seconds=30, deadline_seconds=60)
    deadline_at = run.deadline_at
    staged_snapshot = create_snapshot(conn, session_id, run.id, T0)
    source_id = create_research_source(
        conn, session_id, "rbnz_news", {}, ResearchSourceOrigin.OWNER, T0).id
    staged_item = upsert_evidence_item_if_claimed(
        conn, run.id, run.claim_token, session_id, source_id, "e1", "RBNZ raises rate",
        "https://x/1", EvidenceQuality.REPORTING, T0, snippet="staged, never cancelled")
    assert staged_item is not None
    add_snapshot_items(conn, staged_snapshot.id, [staged_item.id], T0)

    past_deadline = deadline_at + timedelta(seconds=10)
    recovery = recover_expired_research_runs(conn, past_deadline)
    assert recovery.requeued_count == 0
    assert recovery.deadline_exceeded_count == 1

    final = get_research_run(conn, run.id)
    assert final.status == ResearchRunStatus.FAILED
    raw_row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert raw_row["error_code"] == "deadline_exceeded"
    # the staged evidence is stranded, exactly as before this run's failure -- never sealed
    assert get_latest_snapshot(conn, session_id).status == EvidenceSnapshotStatus.BUILDING


# ============================================================================
# Crash recovery: resuming a run whose PRIOR attempt already won atomic finalization
# ============================================================================

def _sealed_run_scenario(conn):
    """A claimed run whose own Evidence Snapshot has already been sealed and pinned to an
    Evidence State Revision via the exact same atomic finalize_snapshot_if_claimed write
    real finalization uses -- standing in for "a prior worker attempt of this exact run
    already won finalization, then crashed or lost its claim before ever synthesizing or
    completing the run." Returns (session_id, run, snapshot, finalized, item)."""
    session_id, run = _claimed_run(conn)
    source_id = create_research_source(
        conn, session_id, "rbnz_news", {}, ResearchSourceOrigin.OWNER, T0).id
    snapshot = create_snapshot(conn, session_id, run.id, T0)
    item = upsert_evidence_item_if_claimed(
        conn, run.id, run.claim_token, session_id, source_id, "e1", "RBNZ raises rate",
        "https://x/1", EvidenceQuality.REPORTING, T0, snippet="already-finalized evidence")
    assert item is not None
    append_result = add_snapshot_items_if_claimed(
        conn, run.id, run.claim_token, session_id, snapshot.id, [item.id], T0)
    assert append_result.ok
    finalized = finalize_snapshot_if_claimed(
        conn, run.id, run.claim_token, session_id, snapshot.id, [], [item.id], T0)
    assert finalized.ok
    return session_id, run, snapshot, finalized, item


def _reclaim(conn, run):
    """Simulates a wholly separate worker reclaiming `run` after a crash: gives back the
    (still-live, in these tests) claim and reclaims it fresh -- a new claim_token, the same
    fixed deadline_at (COALESCEd), exactly like recover_expired_research_runs + a fresh
    claim_research_run already do in production."""
    assert requeue_research_run(conn, run.id, run.claim_token)
    new_lease = claim_research_run(conn, run.id, T0, lease_seconds=600, deadline_seconds=1200)
    assert new_lease is not None
    assert new_lease.run.claim_token != run.claim_token
    return new_lease.run


@pytest.mark.asyncio
async def test_reclaimed_run_with_sealed_snapshot_and_no_synthesis_produces_exactly_one_synthesis(
        conn):
    """Task C crash window #1: finalization exists (sealed snapshot + pinned revision), no
    synthesis yet, and the run is reclaimed by a wholly separate worker. Orchestration must
    resume straight from that sealed snapshot/revision -- no re-collection, no second snapshot,
    no re-clustering, no second revision -- and produce exactly one Research Synthesis."""
    session_id, run, snapshot, finalized, _item = _sealed_run_scenario(conn)
    reclaimed_run = _reclaim(conn, run)

    connectors = {"rbnz_news": _FakeConnector(items=[])}
    ai_script = _AIScript(plan_responses=[], sufficiency_responses=[])

    outcome = await _run_orchestration(
        conn, session_id, reclaimed_run, connectors=connectors, ai_script=ai_script)

    assert outcome.status == RunOutcomeStatus.RESUMED_SEALED_SNAPSHOT
    assert outcome.snapshot_id == snapshot.id
    assert outcome.evidence_state_revision_id == finalized.revision.id
    assert outcome.synthesis_id is not None
    # never re-entered collection or assessment
    assert ai_script.plan_calls == []
    assert ai_script.sufficiency_calls == []
    assert connectors["rbnz_news"].call_count == 0

    # exactly one of each -- never a second snapshot/revision/cluster set/synthesis
    assert len(list_snapshots(conn, session_id)) == 1
    assert len(list_evidence_state_revisions(conn, session_id)) == 1
    assert len(list_evidence_clusters(conn, snapshot.id)) == len(finalized.clusters)
    assert len(list_syntheses(conn, session_id)) == 1

    run_row = get_research_run(conn, run.id)
    assert run_row.status == ResearchRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_reclaimed_run_with_sealed_snapshot_and_existing_synthesis_reuses_it(conn):
    """Task C crash window #2: finalization AND a Research Synthesis for that exact revision
    both already exist, but the run's own terminal complete_research_run write was lost/crashed
    before it landed. A wholly separate worker reclaims the run afterward -- orchestration must
    REUSE that already-persisted synthesis (never a second, redundant AI call, never a duplicate
    synthesis version) and only complete the run."""
    session_id, run, snapshot, finalized, item = _sealed_run_scenario(conn)

    existing_claim = SynthesisClaim(
        text="Prior synthesis claim", section=SynthesisSection.BOTTOM_LINE,
        provenance=ClaimProvenance.EVIDENCE,
        citations=(EvidenceCitation(item.id, item.citation_number),))
    existing_synthesis = create_synthesis(
        conn, session_id, finalized.revision.id, SufficiencyState.PARTIAL, (existing_claim,),
        "gpt-5", "en", T0)

    reclaimed_run = _reclaim(conn, run)

    connectors = {"rbnz_news": _FakeConnector(items=[])}
    ai_script = _AIScript(plan_responses=[], sufficiency_responses=[])

    outcome = await _run_orchestration(
        conn, session_id, reclaimed_run, connectors=connectors, ai_script=ai_script)

    assert outcome.status == RunOutcomeStatus.RESUMED_SEALED_SNAPSHOT
    assert outcome.snapshot_id == snapshot.id
    assert outcome.evidence_state_revision_id == finalized.revision.id
    assert outcome.synthesis_id == existing_synthesis.id
    # never any AI call at all -- the existing synthesis is reused, not regenerated
    assert ai_script.calls == []
    assert len(list_syntheses(conn, session_id)) == 1

    run_row = get_research_run(conn, run.id)
    assert run_row.status == ResearchRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_reclaimed_run_resumes_against_newer_curation_revision_when_r1_has_no_synthesis(
        conn):
    """Task C crash window #3: R1 (the atomic finalization revision, the EARLIEST revision for
    this sealed snapshot) has NO synthesis yet, but Owner curation built a newer revision R2 for
    this SAME sealed snapshot (`research.synthesis.exclude_evidence_item`) while the run sat
    crashed/pending reclaim. `research.synthesis.generate_synthesis` requires the Research
    Session's own latest revision and would correctly reject the now-stale R1 -- resume must
    instead target R2, producing exactly one synthesis pinned to it: never SYNTHESIS_FAILED, and
    never a second snapshot or finalization (cluster/seal/revision) write."""
    session_id, run = _claimed_run(conn)
    source_id = create_research_source(
        conn, session_id, "rbnz_news", {}, ResearchSourceOrigin.OWNER, T0).id
    snapshot = create_snapshot(conn, session_id, run.id, T0)
    item1 = upsert_evidence_item_if_claimed(
        conn, run.id, run.claim_token, session_id, source_id, "e1", "RBNZ raises rate",
        "https://x/1", EvidenceQuality.REPORTING, T0, snippet="already-finalized evidence")
    item2 = upsert_evidence_item_if_claimed(
        conn, run.id, run.claim_token, session_id, source_id, "e2", "RBNZ commentary",
        "https://x/2", EvidenceQuality.REPORTING, T0, snippet="second finalized item")
    assert item1 is not None and item2 is not None
    append_result = add_snapshot_items_if_claimed(
        conn, run.id, run.claim_token, session_id, snapshot.id, [item1.id, item2.id], T0)
    assert append_result.ok
    finalized = finalize_snapshot_if_claimed(
        conn, run.id, run.claim_token, session_id, snapshot.id, [], [item1.id, item2.id], T0)
    assert finalized.ok
    r1 = finalized.revision
    # No synthesis exists for R1 (or at all) yet -- the crash happened before this run's prior
    # attempt ever got to generate one.
    assert list_syntheses(conn, session_id) == []

    # Owner curation excludes item2 while the run sits crashed/pending reclaim -- builds R2 for
    # the SAME sealed snapshot (a sealed snapshot's own membership never changes; only which of
    # its members are "active" is re-derived).
    r2 = synth.exclude_evidence_item(conn, session_id, item2.id, T0, note="not relevant")
    assert r2.id != r1.id
    assert r2.snapshot_id == snapshot.id
    assert set(r2.evidence_item_ids) == {item1.id}

    reclaimed_run = _reclaim(conn, run)

    connectors = {"rbnz_news": _FakeConnector(items=[])}
    ai_script = _AIScript(plan_responses=[], sufficiency_responses=[])

    outcome = await _run_orchestration(
        conn, session_id, reclaimed_run, connectors=connectors, ai_script=ai_script)

    assert outcome.status == RunOutcomeStatus.RESUMED_SEALED_SNAPSHOT
    assert outcome.snapshot_id == snapshot.id
    assert outcome.evidence_state_revision_id == r2.id
    assert outcome.synthesis_id is not None

    # never re-entered collection, never a second snapshot/finalization (cluster/seal/revision)
    assert ai_script.plan_calls == []
    assert ai_script.sufficiency_calls == []
    assert len(list_snapshots(conn, session_id)) == 1
    assert len(list_evidence_state_revisions(conn, session_id)) == 2  # R1 + R2, never a third
    assert len(list_evidence_clusters(conn, snapshot.id)) == len(finalized.clusters)

    # exactly one synthesis, pinned to R2 (never the now-stale R1)
    syntheses = list_syntheses(conn, session_id)
    assert len(syntheses) == 1
    assert syntheses[0].evidence_state_revision_id == r2.id
    assert outcome.synthesis_id == syntheses[0].id

    run_row = get_research_run(conn, run.id)
    assert run_row.status == ResearchRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_reclaimed_run_reuses_synthesis_from_any_revision_of_its_snapshot(conn):
    """A run may persist Y2 for curated R2 and crash before terminal completion.

    If later curation creates R3 before reclaim, recovery must find Y2 through the run's sealed
    snapshot rather than checking only R1 and R3. It must reuse Y2 with no new AI calls and never
    append Y3 for the same run.
    """
    session_id, run = _claimed_run(conn)
    source_id = create_research_source(
        conn, session_id, "rbnz_news", {}, ResearchSourceOrigin.OWNER, T0).id
    snapshot = create_snapshot(conn, session_id, run.id, T0)
    item1 = upsert_evidence_item_if_claimed(
        conn, run.id, run.claim_token, session_id, source_id, "e1", "RBNZ raises rate",
        "https://x/1", EvidenceQuality.REPORTING, T0, snippet="first item")
    item2 = upsert_evidence_item_if_claimed(
        conn, run.id, run.claim_token, session_id, source_id, "e2", "RBNZ commentary",
        "https://x/2", EvidenceQuality.REPORTING, T0, snippet="second item")
    assert item1 is not None and item2 is not None
    append_result = add_snapshot_items_if_claimed(
        conn, run.id, run.claim_token, session_id, snapshot.id, [item1.id, item2.id], T0)
    assert append_result.ok
    finalized = finalize_snapshot_if_claimed(
        conn, run.id, run.claim_token, session_id, snapshot.id, [],
        [item1.id, item2.id], T0)
    assert finalized.ok

    r2 = synth.exclude_evidence_item(
        conn, session_id, item2.id, T0 + timedelta(seconds=1), note="temporarily excluded")
    existing_claim = SynthesisClaim(
        text="Prior synthesis claim", section=SynthesisSection.BOTTOM_LINE,
        provenance=ClaimProvenance.EVIDENCE,
        citations=(EvidenceCitation(item1.id, item1.citation_number),))
    existing_synthesis = create_synthesis(
        conn, session_id, r2.id, SufficiencyState.PARTIAL, (existing_claim,),
        "gpt-5", "en", T0 + timedelta(seconds=2))
    r3 = synth.restore_evidence_item(
        conn, session_id, item2.id, T0 + timedelta(seconds=3))
    assert r3.id != r2.id

    reclaimed_run = _reclaim(conn, run)
    connectors = {"rbnz_news": _FakeConnector(items=[])}
    ai_script = _AIScript(plan_responses=[], sufficiency_responses=[])

    outcome = await _run_orchestration(
        conn, session_id, reclaimed_run, connectors=connectors, ai_script=ai_script)

    assert outcome.status == RunOutcomeStatus.RESUMED_SEALED_SNAPSHOT
    assert outcome.snapshot_id == snapshot.id
    assert outcome.evidence_state_revision_id == r2.id
    assert outcome.synthesis_id == existing_synthesis.id
    assert ai_script.calls == []
    assert [s.id for s in list_syntheses(conn, session_id)] == [existing_synthesis.id]
    assert get_research_run(conn, run.id).status == ResearchRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_reclaimed_cancelled_run_with_sealed_snapshot_terminalizes_as_cancelled(conn):
    """Cancellation semantics survive the sealed-snapshot resume path: a run reclaimed with
    already-sealed evidence and a persisted cancel_requested flag terminalizes according to
    that flag (CANCELLED, not COMPLETED) -- the sealed snapshot and revision are never
    discarded, but a cancelled run never gets a NEW Research Synthesis either (no different
    from any other new AI-authored write cancellation blocks): admit_synthesis_if_claimed's own
    fresh, lock-held check finds cancel_requested already set and skips both LLM calls
    entirely, regardless of how much of the run's own budget remains."""
    session_id, run, snapshot, finalized, _item = _sealed_run_scenario(conn)
    request_cancel_research_run(conn, run.id)
    reclaimed_run = _reclaim(conn, run)

    connectors = {"rbnz_news": _FakeConnector(items=[])}
    ai_script = _AIScript(plan_responses=[], sufficiency_responses=[])

    outcome = await _run_orchestration(
        conn, session_id, reclaimed_run, connectors=connectors, ai_script=ai_script)

    assert outcome.status == RunOutcomeStatus.CANCELLED_WITH_EVIDENCE
    assert outcome.snapshot_id == snapshot.id
    assert outcome.evidence_state_revision_id == finalized.revision.id
    assert outcome.synthesis_id is None
    assert ai_script.calls == []
    assert list_syntheses(conn, session_id) == []

    run_row = get_research_run(conn, run.id)
    assert run_row.status == ResearchRunStatus.CANCELLED


@pytest.mark.asyncio
async def test_reclaimed_run_with_sealed_snapshot_missing_revision_fails_explicitly(conn):
    """Because finalize_snapshot_if_claimed always creates the seal and its Evidence State
    Revision atomically, a sealed run snapshot with NO matching revision at all can only mean a
    data-integrity violation, never a legitimate state to recover from -- this must be an
    explicit typed failure (the run FAILED with a specific error_code), never a silently-minted
    replacement revision."""
    session_id, run = _claimed_run(conn)
    snapshot = create_snapshot(conn, session_id, run.id, T0)
    seal_result = conn.execute(
        "UPDATE research_snapshots SET status = 'sealed', sealed_at = ? WHERE id = ?",
        (T0.isoformat(), snapshot.id))
    conn.commit()
    assert seal_result.rowcount == 1

    connectors = {"rbnz_news": _FakeConnector(items=[])}
    ai_script = _AIScript(plan_responses=[], sufficiency_responses=[])

    outcome = await _run_orchestration(conn, session_id, run, connectors=connectors,
                                        ai_script=ai_script)

    assert outcome.status == RunOutcomeStatus.SEALED_SNAPSHOT_INVALID
    assert outcome.snapshot_id is None
    assert outcome.evidence_state_revision_id is None
    assert outcome.synthesis_id is None
    assert ai_script.calls == []

    run_row = get_research_run(conn, run.id)
    assert run_row.status == ResearchRunStatus.FAILED
    raw_row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert raw_row["error_code"] == "sealed_snapshot_missing_revision"


@pytest.mark.asyncio
async def test_reclaimed_run_with_sealed_snapshot_missing_revision_fails_even_when_cancelled(
        conn):
    """`_resume_sealed_run`'s missing-revision check calls `complete_research_run_if_claimed`
    with `honor_cancel=False` precisely so this data-integrity failure is never silently hidden
    behind a cancellation that merely happened to race it: even a cancelled run must still
    surface SEALED_SNAPSHOT_INVALID/FAILED here, never CANCELLED."""
    session_id, run = _claimed_run(conn)
    snapshot = create_snapshot(conn, session_id, run.id, T0)
    seal_result = conn.execute(
        "UPDATE research_snapshots SET status = 'sealed', sealed_at = ? WHERE id = ?",
        (T0.isoformat(), snapshot.id))
    conn.commit()
    assert seal_result.rowcount == 1
    assert request_cancel_research_run(conn, run.id) is True

    connectors = {"rbnz_news": _FakeConnector(items=[])}
    ai_script = _AIScript(plan_responses=[], sufficiency_responses=[])

    outcome = await _run_orchestration(conn, session_id, run, connectors=connectors,
                                        ai_script=ai_script)

    assert outcome.status == RunOutcomeStatus.SEALED_SNAPSHOT_INVALID
    assert outcome.snapshot_id is None
    assert outcome.evidence_state_revision_id is None
    assert outcome.synthesis_id is None
    assert ai_script.calls == []

    run_row = get_research_run(conn, run.id)
    assert run_row.status == ResearchRunStatus.FAILED
    raw_row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert raw_row["error_code"] == "sealed_snapshot_missing_revision"
