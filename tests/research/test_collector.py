# tests/research/test_collector.py
from datetime import datetime, timezone


from beehive.connectors.base import RawItem
from beehive.domain.research import EvidenceQuality, ResearchSource, ResearchSourceOrigin
from beehive.research.collector import (Candidate, CollectionOutcome, SourceFailure, collect,
                                         quality_for_connector_type)

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)


def _source(source_id, connector_type="google_news_query", config=None):
    return ResearchSource(
        id=source_id, session_id=1, connector_type=connector_type,
        config=config or {"query": "q"}, origin=ResearchSourceOrigin.PLAN)


class _FakeConnector:
    def __init__(self, items=None, raises=None):
        self._items = items or []
        self._raises = raises
        self.received_configs = []

    def validate_config(self, config):
        pass

    def fetch(self, config):
        self.received_configs.append(config)
        if self._raises is not None:
            raise self._raises
        return self._items


def _raw_item(external_id="e1", title="T", url="https://x/1", body=""):
    return RawItem(external_id=external_id, title=title, url=url, body=body, created_at=T0)


# ============================================================================
# quality_for_connector_type
# ============================================================================

def test_official_feeds_are_primary_quality():
    for connector_type in ("rbnz_news", "nz_government_news", "federal_reserve_news"):
        assert quality_for_connector_type(connector_type) == EvidenceQuality.PRIMARY


def test_google_news_is_aggregator_quality():
    assert quality_for_connector_type("google_news_query") == EvidenceQuality.AGGREGATOR


def test_reddit_and_hackernews_are_community_quality():
    for connector_type in ("reddit_subreddit", "hackernews_stories", "hackernews_query"):
        assert quality_for_connector_type(connector_type) == EvidenceQuality.COMMUNITY


def test_unknown_connector_type_falls_back_to_reporting():
    assert quality_for_connector_type("something_unrecognized") == EvidenceQuality.REPORTING


# ============================================================================
# collect(): happy path
# ============================================================================

def test_collect_happy_path_tags_candidates_with_source_and_quality():
    source = _source(10, connector_type="rbnz_news", config={})
    connector = _FakeConnector(items=[_raw_item("e1"), _raw_item("e2")])

    outcome = collect([source], connector_resolver=lambda _type: connector)

    assert isinstance(outcome, CollectionOutcome)
    assert len(outcome.candidates) == 2
    for candidate in outcome.candidates:
        assert isinstance(candidate, Candidate)
        assert candidate.research_source_id == 10
        assert candidate.connector_type == "rbnz_news"
        assert candidate.quality == EvidenceQuality.PRIMARY
    assert outcome.failures == ()
    assert not outcome.all_sources_failed


def test_collect_with_no_sources_returns_empty_outcome():
    outcome = collect([], connector_resolver=lambda _type: _FakeConnector())
    assert outcome.candidates == ()
    assert outcome.failures == ()
    assert not outcome.all_sources_failed


# ============================================================================
# collect(): exact connector execution from validated plan
# ============================================================================

def test_collect_calls_the_resolved_connectors_fetch_with_the_sources_own_config():
    source_a = _source(1, connector_type="google_news_query", config={"query": "rbnz rates"})
    source_b = _source(2, connector_type="reddit_subreddit", config={"subreddit": "newzealand"})
    connector_a = _FakeConnector(items=[_raw_item("a1")])
    connector_b = _FakeConnector(items=[_raw_item("b1")])
    resolved_types = []

    def resolver(connector_type):
        resolved_types.append(connector_type)
        return {"google_news_query": connector_a, "reddit_subreddit": connector_b}[connector_type]

    outcome = collect([source_a, source_b], connector_resolver=resolver)

    assert resolved_types == ["google_news_query", "reddit_subreddit"]
    assert connector_a.received_configs == [{"query": "rbnz rates"}]
    assert connector_b.received_configs == [{"subreddit": "newzealand"}]
    assert len(outcome.candidates) == 2


def test_collect_never_calls_a_connector_not_named_by_a_source():
    source = _source(1, connector_type="rbnz_news", config={})
    connector = _FakeConnector(items=[_raw_item("e1")])
    called_with = []

    def resolver(connector_type):
        called_with.append(connector_type)
        return connector

    collect([source], connector_resolver=resolver)
    assert called_with == ["rbnz_news"]


# ============================================================================
# collect(): per-source failure isolation
# ============================================================================

def test_one_failing_source_does_not_abort_the_others():
    good_source = _source(1, connector_type="rbnz_news", config={})
    bad_source = _source(2, connector_type="google_news_query", config={"query": "x"})
    good_connector = _FakeConnector(items=[_raw_item("g1"), _raw_item("g2")])
    bad_connector = _FakeConnector(raises=RuntimeError("feed unreachable"))

    def resolver(connector_type):
        return {"rbnz_news": good_connector, "google_news_query": bad_connector}[connector_type]

    outcome = collect([good_source, bad_source], connector_resolver=resolver)

    assert len(outcome.candidates) == 2
    assert all(c.research_source_id == 1 for c in outcome.candidates)
    assert len(outcome.failures) == 1
    failure = outcome.failures[0]
    assert isinstance(failure, SourceFailure)
    assert failure.research_source_id == 2
    assert failure.connector_type == "google_news_query"
    assert failure.error_code == "RuntimeError"
    assert "feed unreachable" in failure.detail
    assert not outcome.all_sources_failed


def test_connector_resolution_failure_is_also_isolated():
    source = _source(1, connector_type="rbnz_news", config={})

    def resolver(_connector_type):
        raise ValueError("unknown Source type")

    outcome = collect([source], connector_resolver=resolver)
    assert outcome.candidates == ()
    assert len(outcome.failures) == 1
    assert outcome.failures[0].error_code == "ValueError"


def test_all_sources_failing_reports_all_sources_failed():
    source_a = _source(1, connector_type="rbnz_news", config={})
    source_b = _source(2, connector_type="nz_government_news", config={})
    connector = _FakeConnector(raises=RuntimeError("down"))

    outcome = collect([source_a, source_b], connector_resolver=lambda _t: connector)

    assert outcome.candidates == ()
    assert len(outcome.failures) == 2
    assert outcome.all_sources_failed


def test_failure_detail_is_bounded_length():
    source = _source(1, connector_type="rbnz_news", config={})
    connector = _FakeConnector(raises=RuntimeError("x" * 5000))

    outcome = collect([source], connector_resolver=lambda _t: connector)

    assert len(outcome.failures[0].detail) <= 500


# ============================================================================
# collect(): candidate cap per source
# ============================================================================

def test_candidates_per_source_are_capped():
    source = _source(1, connector_type="rbnz_news", config={})
    items = [_raw_item(f"e{i}") for i in range(100)]
    connector = _FakeConnector(items=items)

    outcome = collect([source], connector_resolver=lambda _t: connector, max_candidates_per_source=5)

    assert len(outcome.candidates) == 5
