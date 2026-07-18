# tests/research/test_enrichment.py
from datetime import datetime, timezone

import pytest

from beehive.connectors.base import RawItem
from beehive.db.connection import connect, init_schema
from beehive.db.evidence_items import get_evidence_item, upsert_evidence_item_if_claimed
from beehive.db.research_runs import claim_research_run, enqueue_research_run
from beehive.db.research_sessions import create_research_session
from beehive.db.research_sources import create_research_source
from beehive.deep_read.fetch import FetchFailure, FetchFailureReason, FetchedArticle
from beehive.domain.research import EvidenceQuality, ResearchSourceOrigin
from beehive.research.collector import Candidate
from beehive.research.enrichment import (DeepFetchStatus, persist_candidate_snippet,
                                          project_for_prompt, reserve_and_deep_fetch,
                                          select_relevant_candidates)
from beehive.research.limits import MAX_EVIDENCE_TEXT_CHARS_IN_PROMPT

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def session_and_source(conn):
    session_id = create_research_session(conn, "Q", T0).id
    source_id = create_research_source(
        conn, session_id, "rbnz_news", {}, ResearchSourceOrigin.PLAN, T0).id
    return session_id, source_id


def _candidate(source_id, external_id="e1", title="T", url="https://x/1", body="body",
               quality=EvidenceQuality.PRIMARY, source_topic_hint=None):
    return Candidate(
        research_source_id=source_id, connector_type="rbnz_news", quality=quality,
        raw_item=RawItem(external_id=external_id, title=title, url=url, body=body, created_at=T0),
        source_topic_hint=source_topic_hint)


def _complete_html(paragraph="Full article body. " * 50):
    return f"<html><body><p>{paragraph}</p></body></html>"


class _FakeFetcher:
    """Injected in place of the real SSRF-safe ArticleFetcher. Tracks calls for reuse
    assertions and returns a scripted sequence of FetchOutcomes."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls: list[str] = []

    def fetch(self, url):
        self.calls.append(url)
        return self._outcomes.pop(0)


class _CallbackFetcher:
    """Fetcher whose outcome is produced by a callback rather than a scripted list, so a test
    can perform side effects -- e.g. simulating a concurrent claim reclaim on a second DB
    connection -- at exactly the moment the network fetch would run, before returning the
    FetchOutcome."""

    def __init__(self, outcome_fn):
        self._outcome_fn = outcome_fn
        self.calls: list[str] = []

    def fetch(self, url):
        self.calls.append(url)
        return self._outcome_fn(url)


def _run(conn, session_id):
    return enqueue_research_run(conn, session_id, T0)


def _claim(conn, run_id):
    lease = claim_research_run(conn, run_id, T0, lease_seconds=120, deadline_seconds=1200)
    return lease.run


def _claimed_run(conn, session_id):
    """Enqueues and claims a fresh Research Run in one step -- most tests here only care about
    having a live (run_id, claim_token) pair to fence writes against."""
    return _claim(conn, _run(conn, session_id).id)


# ============================================================================
# select_relevant_candidates
# ============================================================================

def test_select_relevant_candidates_ranks_by_keyword_overlap():
    c1 = _candidate(1, "e1", title="Reserve Bank interest rate decision", body="")
    c2 = _candidate(1, "e2", title="Completely unrelated sports news", body="")
    selected = select_relevant_candidates([c2, c1], "Reserve Bank interest rate", limit=1)
    assert selected == [c1]


def test_select_relevant_candidates_ties_broken_by_quality_then_order():
    primary = _candidate(1, "p", title="x", body="", quality=EvidenceQuality.PRIMARY)
    community = _candidate(1, "c", title="x", body="", quality=EvidenceQuality.COMMUNITY)
    selected = select_relevant_candidates([community, primary], "irrelevant question", limit=2)
    assert selected == [primary, community]


def test_select_relevant_candidates_respects_limit():
    candidates = [_candidate(1, f"e{i}") for i in range(5)]
    assert len(select_relevant_candidates(candidates, "q", limit=2)) == 2


def test_select_relevant_candidates_zero_limit_is_empty():
    candidates = [_candidate(1, "e1")]
    assert select_relevant_candidates(candidates, "q", limit=0) == []


def test_select_relevant_candidates_empty_input_is_empty():
    assert select_relevant_candidates([], "q", limit=5) == []


def test_select_relevant_candidates_uses_source_topic_hint_when_question_is_untokenizable():
    """Regression test for the bug where a non-Latin-script (e.g. Chinese) Research Question
    tokenizes to nothing, so every Candidate tied at relevance=0 and AGGREGATOR-tier
    google_news_query results always lost the quality tie-break to COMMUNITY-tier noise. A
    hint-matched google_news_query Candidate must now outrank a hint-less, quality-favored
    hackernews_stories Candidate whose title/body do not otherwise relate to anything."""
    aggregator_hit = _candidate(
        1, "g1", title="GPT-5.3-Codex tops SWE-bench coding leaderboard", body="",
        quality=EvidenceQuality.AGGREGATOR, source_topic_hint="best coding model SWE-bench")
    community_noise = _candidate(
        2, "h1", title="Show HN: I built a JPEG compressor", body="",
        quality=EvidenceQuality.COMMUNITY, source_topic_hint=None)
    selected = select_relevant_candidates(
        [community_noise, aggregator_hit], "写程序最好用的模型是什么", limit=1)
    assert selected == [aggregator_hit]


def test_select_relevant_candidates_falls_back_to_quality_rank_when_hint_also_absent():
    """When neither the Research Question nor the Candidate's own source has any topic hint
    (e.g. hackernews_stories' plain {"feed": "top"} config against an untokenizable question),
    every Candidate still ties at relevance=0 and selection correctly falls back to
    _QUALITY_RANK, exactly as it did before source_topic_hint existed."""
    primary = _candidate(1, "p", title="x", body="", quality=EvidenceQuality.PRIMARY)
    community = _candidate(2, "c", title="x", body="", quality=EvidenceQuality.COMMUNITY)
    selected = select_relevant_candidates([community, primary], "写程序最好用的模型是什么", limit=2)
    assert selected == [primary, community]


def test_select_relevant_candidates_reddit_subreddit_name_counts_as_a_topic_hint():
    on_topic = _candidate(
        1, "r1", title="Artificial general intelligence coding benchmarks compared", body="",
        quality=EvidenceQuality.COMMUNITY, source_topic_hint="artificial")
    off_topic = _candidate(
        2, "r2", title="Community meetup announcement for next week", body="",
        quality=EvidenceQuality.COMMUNITY, source_topic_hint=None)
    selected = select_relevant_candidates([off_topic, on_topic], "写程序最好用的模型是什么", limit=1)
    assert selected == [on_topic]


def test_select_relevant_candidates_topic_hint_does_not_regress_english_question_ranking():
    """A source_topic_hint only ever adds signal (union of tokens): an English-question ranking
    that already worked correctly must keep working the same way once every Candidate also
    carries a hint."""
    c1 = _candidate(
        1, "e1", title="Reserve Bank interest rate decision", body="",
        source_topic_hint="reserve bank rate decision")
    c2 = _candidate(
        1, "e2", title="Completely unrelated sports news", body="",
        source_topic_hint="football scores")
    selected = select_relevant_candidates([c2, c1], "Reserve Bank interest rate", limit=1)
    assert selected == [c1]


# ============================================================================
# project_for_prompt: bounded projection
# ============================================================================

def test_project_for_prompt_prefers_full_text_over_snippet(conn, session_and_source):
    session_id, source_id = session_and_source
    run = _claimed_run(conn, session_id)
    item = persist_candidate_snippet(
        conn, run.id, run.claim_token, session_id, T0, _candidate(source_id, body="short snippet"))
    item = item.__class__(**{**item.__dict__, "full_text": "the full article text"})
    assert project_for_prompt(item) == "the full article text"


def test_project_for_prompt_falls_back_to_snippet_when_no_full_text(conn, session_and_source):
    session_id, source_id = session_and_source
    run = _claimed_run(conn, session_id)
    item = persist_candidate_snippet(
        conn, run.id, run.claim_token, session_id, T0, _candidate(source_id, body="just a snippet"))
    assert project_for_prompt(item) == "just a snippet"


def test_project_for_prompt_is_bounded_to_max_chars(conn, session_and_source):
    session_id, source_id = session_and_source
    run = _claimed_run(conn, session_id)
    long_text = "w" * (MAX_EVIDENCE_TEXT_CHARS_IN_PROMPT + 5000)
    item = persist_candidate_snippet(
        conn, run.id, run.claim_token, session_id, T0, _candidate(source_id, body="s"))
    item = item.__class__(**{**item.__dict__, "full_text": long_text})
    projected = project_for_prompt(item)
    assert len(projected) == MAX_EVIDENCE_TEXT_CHARS_IN_PROMPT


# ============================================================================
# persist_candidate_snippet
# ============================================================================

def test_persist_candidate_snippet_creates_evidence_item(conn, session_and_source):
    session_id, source_id = session_and_source
    run = _claimed_run(conn, session_id)
    item = persist_candidate_snippet(
        conn, run.id, run.claim_token, session_id, T0, _candidate(source_id))
    assert item.session_id == session_id
    assert item.research_source_id == source_id
    assert item.snippet == "body"
    assert item.full_text is None
    assert item.citation_number == 1


def test_persist_candidate_snippet_is_idempotent_on_citation_number(conn, session_and_source):
    session_id, source_id = session_and_source
    run = _claimed_run(conn, session_id)
    first = persist_candidate_snippet(
        conn, run.id, run.claim_token, session_id, T0, _candidate(source_id, "e1"))
    second = persist_candidate_snippet(
        conn, run.id, run.claim_token, session_id, T0, _candidate(source_id, "e1"))
    assert first.id == second.id
    assert first.citation_number == second.citation_number


def test_persist_candidate_snippet_preserves_existing_full_text_from_a_prior_deep_fetch(
        conn, session_and_source):
    session_id, source_id = session_and_source
    run = _claimed_run(conn, session_id)
    item = persist_candidate_snippet(
        conn, run.id, run.claim_token, session_id, T0, _candidate(source_id, "e1"))
    fetcher = _FakeFetcher(outcomes=[FetchedArticle(
        url=item.url, status_code=200, content_type="text/html", html=_complete_html(),
        truncated=False)])
    deep = reserve_and_deep_fetch(
        conn, run.id, run.claim_token, session_id, item, T0, fetcher=fetcher)
    assert deep.evidence_item.full_text

    # Re-collecting the same Candidate in a later round must not wipe out the full_text the
    # deep fetch above already durably persisted for this same canonical row.
    refreshed = persist_candidate_snippet(
        conn, run.id, run.claim_token, session_id, T0, _candidate(source_id, "e1"))
    assert refreshed.full_text == deep.evidence_item.full_text


def test_persist_candidate_snippet_returns_none_and_writes_nothing_when_claim_is_stale(
        conn, session_and_source):
    session_id, source_id = session_and_source
    run = _claimed_run(conn, session_id)

    item = persist_candidate_snippet(
        conn, run.id, "not-the-real-token", session_id, T0, _candidate(source_id, "e1"))

    assert item is None
    assert conn.execute("SELECT COUNT(*) AS n FROM research_evidence_items").fetchone()["n"] == 0


def test_persist_candidate_snippet_stale_claim_does_not_overwrite_an_existing_row(
        conn, session_and_source):
    session_id, source_id = session_and_source
    run = _claimed_run(conn, session_id)
    original = persist_candidate_snippet(
        conn, run.id, run.claim_token, session_id, T0,
        _candidate(source_id, "e1", title="Original title", body="original snippet"))

    # Simulate this worker's claim having been recovered and reclaimed by someone else.
    conn.execute(
        "UPDATE research_runs SET claim_token = 'someone-elses-token' WHERE id = ?", (run.id,))
    conn.commit()

    result = persist_candidate_snippet(
        conn, run.id, run.claim_token, session_id, T0,
        _candidate(source_id, "e1", title="Stale worker's title", body="stale snippet"))

    assert result is None
    unchanged = get_evidence_item(conn, original.id)
    assert unchanged.title == "Original title"
    assert unchanged.snippet == "original snippet"


# ============================================================================
# reserve_and_deep_fetch: reservation-before-I/O
# ============================================================================

def test_reservation_denied_never_calls_the_fetcher(conn, session_and_source):
    session_id, source_id = session_and_source
    run = _run(conn, session_id)
    claimed = _claim(conn, run.id)
    item = persist_candidate_snippet(
        conn, claimed.id, claimed.claim_token, session_id, T0, _candidate(source_id))
    fetcher = _FakeFetcher(outcomes=[])

    # Wrong claim_token => reservation must fail before any fetch happens.
    result = reserve_and_deep_fetch(
        conn, claimed.id, "not-the-real-token", session_id, item, T0, fetcher=fetcher)

    assert result.status == DeepFetchStatus.RESERVATION_DENIED
    assert fetcher.calls == []
    assert result.evidence_item == item


def test_successful_reservation_calls_fetcher_with_evidence_item_url(conn, session_and_source):
    session_id, source_id = session_and_source
    run = _claimed_run(conn, session_id)
    item = persist_candidate_snippet(
        conn, run.id, run.claim_token, session_id, T0,
        _candidate(source_id, url="https://x/full"))
    fetcher = _FakeFetcher(outcomes=[FetchedArticle(
        url="https://x/full", status_code=200, content_type="text/html",
        html="<html><body><p>" + ("Full article body. " * 50) + "</p></body></html>",
        truncated=False)])

    result = reserve_and_deep_fetch(
        conn, run.id, run.claim_token, session_id, item, T0, fetcher=fetcher)

    assert fetcher.calls == ["https://x/full"]
    assert result.status in (DeepFetchStatus.EXTRACTION_COMPLETE, DeepFetchStatus.EXTRACTION_PARTIAL)
    assert result.evidence_item.full_text


def test_fetch_failure_persists_nothing_further_and_keeps_snippet(conn, session_and_source):
    session_id, source_id = session_and_source
    run = _claimed_run(conn, session_id)
    item = persist_candidate_snippet(
        conn, run.id, run.claim_token, session_id, T0,
        _candidate(source_id, body="original snippet"))
    fetcher = _FakeFetcher(outcomes=[
        FetchFailure(FetchFailureReason.PROHIBITED_ADDRESS, "blocked by SSRF policy")])

    result = reserve_and_deep_fetch(
        conn, run.id, run.claim_token, session_id, item, T0, fetcher=fetcher)

    assert result.status == DeepFetchStatus.FETCH_FAILED
    assert result.fetch_failure_reason == FetchFailureReason.PROHIBITED_ADDRESS
    assert result.evidence_item.snippet == "original snippet"
    assert result.evidence_item.full_text is None


def test_unusable_extraction_retains_snippet_evidence(conn, session_and_source):
    session_id, source_id = session_and_source
    run = _claimed_run(conn, session_id)
    item = persist_candidate_snippet(
        conn, run.id, run.claim_token, session_id, T0,
        _candidate(source_id, body="original snippet"))
    fetcher = _FakeFetcher(outcomes=[FetchedArticle(
        url=item.url, status_code=200, content_type="text/html", html="<html></html>", truncated=False)])

    result = reserve_and_deep_fetch(
        conn, run.id, run.claim_token, session_id, item, T0, fetcher=fetcher)

    assert result.status == DeepFetchStatus.EXTRACTION_UNUSABLE
    assert result.evidence_item.snippet == "original snippet"
    assert result.evidence_item.full_text is None


def test_full_text_over_20k_chars_is_retained_uncapped(conn, session_and_source):
    session_id, source_id = session_and_source
    run = _claimed_run(conn, session_id)
    item = persist_candidate_snippet(
        conn, run.id, run.claim_token, session_id, T0, _candidate(source_id))
    long_paragraph = "This is a long sentence about the research topic. " * 500  # > 20k chars
    assert len(long_paragraph) > 20_000
    fetcher = _FakeFetcher(outcomes=[FetchedArticle(
        url=item.url, status_code=200, content_type="text/html",
        html=f"<html><body><p>{long_paragraph}</p></body></html>", truncated=False)])

    result = reserve_and_deep_fetch(
        conn, run.id, run.claim_token, session_id, item, T0, fetcher=fetcher)

    assert result.status == DeepFetchStatus.EXTRACTION_COMPLETE
    assert len(result.evidence_item.full_text) > 20_000


def test_deep_fetch_reserves_exactly_one_slot_per_call(conn, session_and_source):
    session_id, source_id = session_and_source
    run = _claimed_run(conn, session_id)
    item = persist_candidate_snippet(
        conn, run.id, run.claim_token, session_id, T0, _candidate(source_id))
    fetcher = _FakeFetcher(outcomes=[FetchedArticle(
        url=item.url, status_code=200, content_type="text/html", html="<html></html>", truncated=False)])

    reserve_and_deep_fetch(conn, run.id, run.claim_token, session_id, item, T0, fetcher=fetcher)

    row = conn.execute("SELECT deep_fetch_count FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert row["deep_fetch_count"] == 1


# ============================================================================
# reserve_and_deep_fetch: claim lost mid-fetch cannot clobber a fresher claim's write
# ============================================================================

def test_deep_fetch_claim_lost_during_fetch_returns_claim_lost_status(
        tmp_path, conn, session_and_source):
    session_id, source_id = session_and_source
    run = _claimed_run(conn, session_id)
    item = persist_candidate_snippet(
        conn, run.id, run.claim_token, session_id, T0,
        _candidate(source_id, url="https://x/full"))

    def _reclaim_on_a_second_connection_then_fetch(url):
        # A second, independent connection to the same on-disk database -- simulating another
        # worker recovering this run's expired lease and reclaiming it with a brand-new
        # claim_token while THIS worker's fetch is still in flight.
        other = connect(str(tmp_path / "test.db"))
        init_schema(other)
        other.execute(
            "UPDATE research_runs SET status = 'pending', phase = NULL, "
            "claim_token = NULL, lease_expires_at = NULL WHERE id = ?", (run.id,))
        other.commit()
        new_lease = claim_research_run(other, run.id, T0, lease_seconds=120, deadline_seconds=1200)
        fresher = upsert_evidence_item_if_claimed(
            other, new_lease.run.id, new_lease.run.claim_token, session_id,
            item.research_source_id, item.external_key, item.title, item.url, item.quality, T0,
            snippet=item.snippet, full_text="fresher text from the new claim",
            raw_metadata=item.raw_metadata)
        assert fresher is not None
        other.close()
        return FetchedArticle(
            url=url, status_code=200, content_type="text/html", html=_complete_html(),
            truncated=False)

    fetcher = _CallbackFetcher(_reclaim_on_a_second_connection_then_fetch)

    result = reserve_and_deep_fetch(
        conn, run.id, run.claim_token, session_id, item, T0, fetcher=fetcher)

    assert result.status == DeepFetchStatus.CLAIM_LOST
    # The unchanged, pre-fetch evidence_item is returned -- the stale worker made no writes.
    assert result.evidence_item == item


def test_deep_fetch_claim_lost_during_fetch_leaves_the_fresher_row_unchanged(
        tmp_path, conn, session_and_source):
    session_id, source_id = session_and_source
    run = _claimed_run(conn, session_id)
    item = persist_candidate_snippet(
        conn, run.id, run.claim_token, session_id, T0,
        _candidate(source_id, url="https://x/full"))

    def _reclaim_on_a_second_connection_then_fetch(url):
        other = connect(str(tmp_path / "test.db"))
        init_schema(other)
        other.execute(
            "UPDATE research_runs SET status = 'pending', phase = NULL, "
            "claim_token = NULL, lease_expires_at = NULL WHERE id = ?", (run.id,))
        other.commit()
        new_lease = claim_research_run(other, run.id, T0, lease_seconds=120, deadline_seconds=1200)
        upsert_evidence_item_if_claimed(
            other, new_lease.run.id, new_lease.run.claim_token, session_id,
            item.research_source_id, item.external_key, item.title, item.url, item.quality, T0,
            snippet=item.snippet, full_text="fresher text from the new claim",
            raw_metadata=item.raw_metadata)
        other.close()
        return FetchedArticle(
            url=url, status_code=200, content_type="text/html", html=_complete_html(),
            truncated=False)

    fetcher = _CallbackFetcher(_reclaim_on_a_second_connection_then_fetch)
    reserve_and_deep_fetch(conn, run.id, run.claim_token, session_id, item, T0, fetcher=fetcher)

    stored = get_evidence_item(conn, item.id)
    assert stored.full_text == "fresher text from the new claim"
