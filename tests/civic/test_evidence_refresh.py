from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from beehive.civic.catalog import BEEHIVE_RELEASES, HANSARD_DEBATES
from beehive.civic.connectors.feeds import CivicFeedConnector
from beehive.civic.connectors.parliament import ParliamentApiConnector
from beehive.civic.evidence_refresh import (
    EvidenceCaptureInput,
    EvidenceRefreshStatus,
    capture_fingerprint,
    plan_evidence_refresh,
    prepare_evidence_refresh,
)
from beehive.civic.production import PartyMediaDocumentHydrator
from beehive.connectors.http import ConnectorHttpErrorKind
from beehive.domain.civic import (
    CivicBatch,
    CivicBatchStatus,
    CivicCandidate,
    CivicCandidateStatus,
    CivicEvidence,
    CivicEvidenceStance,
    CivicEvidenceTier,
)
from tests.civic.civic_stubs import fixture_bytes, http_error, transport_for
from tests.connectors.civic_fixtures_loader import civic_fixture

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
LASTMOD = datetime.fromisoformat("2026-09-04T08:41:29.788+00:00")
URL = "https://www.national.org.nz/news/fixture-announcement"


def _batch():
    return CivicBatch(
        id=1, run_id=1, watch_id=1, status=CivicBatchStatus.BUILDING,
        idempotency_key="fixture-batch", attempt_count=0, max_attempts=3, created_at=NOW,
    )


def _candidate():
    return CivicCandidate(
        id=1, watch_id=1, run_id=1, batch_id=1, dedupe_key="fixture-candidate",
        title="Legacy candidate title", statement="Legacy candidate statement.",
        status=CivicCandidateStatus.VERIFIED, created_at=NOW - timedelta(days=3),
        settled_at=NOW - timedelta(days=3), ai_revision=1,
    )


def _capture():
    return EvidenceCaptureInput(CivicEvidence(
        id=1, candidate_id=1, source_ref="national", external_key=f"national:{URL}",
        url=URL, canonical_url=URL, title="Legacy title",
        tier=CivicEvidenceTier.PARTY_PRIMARY, stance=CivicEvidenceStance.SUPPORTING,
        captured_at=NOW - timedelta(days=3), publisher="New Zealand National Party",
        excerpt="A nonempty legacy excerpt is not proof of a current source capture.",
        published_at=LASTMOD, event_date=date(2026, 9, 4),
        publisher_id="national", source_kind="party-primary", party_slug="national",
        subject_id="national", claim_category="party-timeline-event",
        accessible_full_text=True,
    ))


def _two_captures():
    first = _capture()
    second_url = f"{URL}-followup"
    second = EvidenceCaptureInput(replace(
        first.evidence,
        id=2,
        external_key=f"national:{second_url}",
        url=second_url,
        canonical_url=second_url,
        stance=CivicEvidenceStance.CORROBORATING,
    ))
    return first, second


def _official_capture(document):
    event = document.raw_metadata.get("event_date")
    event_date = (
        date.fromisoformat(event) if event else
        document.published_at.astimezone(ZoneInfo("Pacific/Auckland")).date()
        if document.published_at is not None else None
    )
    return EvidenceCaptureInput(
        replace(
            _capture().evidence,
            source_ref=document.source_key,
            external_key=document.dedupe_key,
            url=document.canonical_url,
            canonical_url=document.canonical_url,
            title=document.title,
            publisher=document.publisher,
            publisher_id=document.source_key,
            excerpt=document.excerpt,
            tier=document.tier,
            party_slug=None,
            source_kind=(
                "parliament" if document.source_key == HANSARD_DEBATES.source_key
                else "government"
            ),
            published_at=document.published_at,
            event_date=event_date,
        ),
        document.raw_metadata,
    )


def _fresh_official_feed_capture():
    transport = transport_for({
        BEEHIVE_RELEASES.endpoint.url: fixture_bytes("beehive_rss.xml"),
    })
    document = CivicFeedConnector(BEEHIVE_RELEASES, transport).fetch(NOW).documents[0]
    return _official_capture(document)


def _fresh_official_event_capture():
    body = (
        b'{"@odata.count":1,"value":[{"id":"fixture-debate","title":"Fixture debate",'
        b'"sittingDate":"2026-09-02T00:00:00Z","subtitle":"A synthetic debate record."}]}'
    )
    transport = transport_for({
        HANSARD_DEBATES.endpoint.url: (body, "application/json"),
    })
    document = ParliamentApiConnector(HANSARD_DEBATES, transport).fetch(NOW).documents[0]
    return _official_capture(document)


async def _prepare(plan, routes, **kwargs):
    transport = transport_for(routes)
    hydrator = PartyMediaDocumentHydrator(transport)

    async def hydrate(document, now):
        return hydrator(document, now)

    result = await prepare_evidence_refresh(
        plan, hydrate=hydrate,
        now_fn=kwargs.get("now_fn", lambda: NOW),
        deadline_at=kwargs.get("deadline_at", NOW + timedelta(minutes=5)),
    )
    return result, transport


async def test_legacy_capture_is_actually_refetched_without_reusing_its_bad_publication():
    candidate = _candidate()
    capture = _capture()
    original_fingerprint = capture_fingerprint([capture])
    plan = plan_evidence_refresh(
        candidate, _batch(), [capture], remaining_hydrations=1,
    )
    assert plan.status is EvidenceRefreshStatus.PLANNED
    assert plan.expected_ai_revision == 1
    assert plan.targets[0].expected_evidence_fingerprint == original_fingerprint
    assert plan.targets[0].document.published_at is None
    assert plan.targets[0].document.excerpt == capture.evidence.excerpt
    result, transport = await _prepare(
        plan, {URL: (civic_fixture("national_article_dates.html"), "text/html")},
    )
    assert result.status is EvidenceRefreshStatus.PREPARED
    assert len(transport.recorded.calls) == 1
    replacement = result.captures[0]
    assert replacement.supersedes_evidence_id == capture.evidence.id
    assert replacement.expected_evidence_fingerprint == original_fingerprint
    assert replacement.document.published_at == datetime.fromisoformat(
        "2026-09-02T23:58:38.731+00:00")
    assert replacement.document.dedupe_key == capture.evidence.external_key
    assert capture.evidence.published_at == LASTMOD
    assert capture.evidence.event_date == date(2026, 9, 4)
    assert candidate.ai_revision == 1
    assert capture_fingerprint([capture]) == original_fingerprint
    assert plan.expected_capture_fingerprint == original_fingerprint


async def test_missing_fresh_date_never_falls_back_to_the_legacy_stored_date():
    plan = plan_evidence_refresh(
        _candidate(), _batch(), [_capture()], remaining_hydrations=1,
    )
    page = (
        "<html><body><article><h1>An undated release</h1><p>"
        + "This is synthetic, attributable source prose without a publication date. " * 5
        + "</p></article></body></html>"
    )
    result, transport = await _prepare(plan, {URL: (page, "text/html")})
    assert result.status is EvidenceRefreshStatus.PREPARED
    assert result.captures[0].document.published_at is None
    assert result.captures[0].document.raw_metadata["source_capture"]["publication"] is None
    assert len(transport.recorded.calls) == 1


async def test_all_required_captures_for_one_candidate_are_prepared_together():
    captures = _two_captures()
    original_fingerprint = capture_fingerprint(captures)
    plan = plan_evidence_refresh(
        _candidate(), _batch(), tuple(reversed(captures)), remaining_hydrations=2,
    )
    page = civic_fixture("national_article_dates.html")
    routes = {
        capture.evidence.canonical_url: (
            page.replace(URL.encode(), capture.evidence.canonical_url.encode()),
            "text/html",
        )
        for capture in captures
    }
    result, transport = await _prepare(plan, routes)

    assert result.status is EvidenceRefreshStatus.PREPARED
    assert [capture.supersedes_evidence_id for capture in result.captures] == [1, 2]
    assert [capture.document.dedupe_key for capture in result.captures] == [
        capture.evidence.external_key for capture in captures
    ]
    assert [capture.expected_evidence_fingerprint for capture in result.captures] == [
        capture_fingerprint((capture,)) for capture in captures
    ]
    assert len(result.attempts) == len(transport.recorded.calls) == 2
    assert all(attempt.error_code is None for attempt in result.attempts)
    assert plan.expected_capture_fingerprint == original_fingerprint
    assert capture_fingerprint(captures) == original_fingerprint
    with pytest.raises(ValueError, match="replace every planned capture together"):
        replace(result, captures=result.captures[:1])
    with pytest.raises(ValueError, match="does not match its planned evidence"):
        replace(
            result,
            captures=(
                replace(result.captures[0], expected_evidence_fingerprint="0" * 64),
                result.captures[1],
            ),
        )


async def test_one_failed_fetch_never_offers_the_other_capture_for_installation():
    captures = _two_captures()
    original_fingerprint = capture_fingerprint(captures)
    plan = plan_evidence_refresh(
        _candidate(), _batch(), captures, remaining_hydrations=2,
    )
    result, transport = await _prepare(plan, {
        URL: (civic_fixture("national_article_dates.html"), "text/html"),
        captures[1].evidence.canonical_url: http_error(ConnectorHttpErrorKind.ACCESS_DENIED),
    })

    assert result.status is EvidenceRefreshStatus.DEFERRED
    assert result.reason == "detail_fetch_access_denied"
    assert result.captures == ()
    assert len(result.attempts) == len(transport.recorded.calls) == 2
    assert result.attempts[0].error_code is None
    assert result.attempts[1].error_code == "detail_fetch_access_denied"
    assert capture_fingerprint(captures) == original_fingerprint


async def test_whole_candidate_budget_is_required_before_any_capture_fetch():
    plan = plan_evidence_refresh(
        _candidate(), _batch(), _two_captures(), remaining_hydrations=1,
    )
    result, transport = await _prepare(plan, {})
    assert result.status is EvidenceRefreshStatus.DEFERRED
    assert result.reason == "source_refresh_hydration_limit"
    assert result.captures == ()
    assert transport.recorded.calls == []


def test_one_refresh_plan_cannot_mix_evidence_from_different_candidates():
    first, second = _two_captures()
    other_candidate = EvidenceCaptureInput(replace(second.evidence, candidate_id=2))
    with pytest.raises(ValueError, match="owned by its Candidate"):
        plan_evidence_refresh(
            _candidate(), _batch(), (first, other_candidate), remaining_hydrations=2,
        )


@pytest.mark.parametrize("state", [
    CivicBatchStatus.SEALED, CivicBatchStatus.QUARANTINED, CivicBatchStatus.DELIVERED,
])
async def test_sealed_and_quarantined_batches_are_never_refetched(state):
    batch = replace(
        _batch(), status=state, content_hash="a" * 64, sealed_at=NOW,
        settled_at=NOW if state is not CivicBatchStatus.SEALED else None,
    )
    plan = plan_evidence_refresh(
        _candidate(), batch, [_capture()], remaining_hydrations=25,
    )
    result, transport = await _prepare(plan, {})
    assert result.status is EvidenceRefreshStatus.PRESERVED
    assert result.captures == ()
    assert transport.recorded.calls == []


async def test_candidate_seal_marker_preserves_content_even_with_a_building_batch_input():
    candidate = replace(_candidate(), sealed_into_batch_at=NOW)
    plan = plan_evidence_refresh(
        candidate, _batch(), [_capture()], remaining_hydrations=25,
    )
    result, transport = await _prepare(plan, {})
    assert result.status is EvidenceRefreshStatus.PRESERVED
    assert transport.recorded.calls == []


async def test_rejected_candidates_are_not_reopened_for_source_refresh():
    candidate = replace(
        _candidate(), status=CivicCandidateStatus.REJECTED, rejection_code="fixture-rejection",
    )
    plan = plan_evidence_refresh(
        candidate, _batch(), [_capture()], remaining_hydrations=25,
    )
    result, transport = await _prepare(plan, {})
    assert result.status is EvidenceRefreshStatus.PRESERVED
    assert transport.recorded.calls == []


async def test_budget_exhaustion_defers_before_any_fetch():
    plan = plan_evidence_refresh(
        _candidate(), _batch(), [_capture()], remaining_hydrations=0,
    )
    result, transport = await _prepare(plan, {})
    assert result.status is EvidenceRefreshStatus.DEFERRED
    assert result.reason == "source_refresh_hydration_limit"
    assert transport.recorded.calls == []


async def test_expired_deadline_performs_no_source_request():
    plan = plan_evidence_refresh(
        _candidate(), _batch(), [_capture()], remaining_hydrations=1,
    )
    result, transport = await _prepare(plan, {}, deadline_at=NOW)
    assert result.status is EvidenceRefreshStatus.DEFERRED
    assert result.reason == "source_refresh_deadline"
    assert transport.recorded.calls == []


async def test_deadline_crossing_does_not_offer_a_capture_for_installation():
    plan = plan_evidence_refresh(
        _candidate(), _batch(), [_capture()], remaining_hydrations=1,
    )
    ticks = iter((NOW, NOW + timedelta(minutes=6)))
    result, transport = await _prepare(
        plan, {URL: (civic_fixture("national_article_dates.html"), "text/html")},
        now_fn=lambda: next(ticks),
    )
    assert result.status is EvidenceRefreshStatus.DEFERRED
    assert result.reason == "source_refresh_deadline"
    assert result.captures == ()
    assert len(result.attempts) == len(transport.recorded.calls) == 1


async def test_current_capture_is_idempotent_and_does_not_refetch():
    original = _capture()
    plan = plan_evidence_refresh(
        _candidate(), _batch(), [original], remaining_hydrations=1,
    )
    refreshed, _ = await _prepare(
        plan, {URL: (civic_fixture("national_article_dates.html"), "text/html")},
    )
    document = refreshed.captures[0].document
    current = EvidenceCaptureInput(
        replace(
            original.evidence, id=2, title=document.title, excerpt=document.excerpt,
            published_at=document.published_at,
        ),
        document.raw_metadata,
    )
    next_plan = plan_evidence_refresh(
        _candidate(), _batch(), [current], remaining_hydrations=0,
    )
    result, transport = await _prepare(next_plan, {})
    assert result.status is EvidenceRefreshStatus.CURRENT
    assert transport.recorded.calls == []


def test_unsupported_legacy_sources_are_deferred_not_silently_reused():
    capture = _capture()
    unsupported = EvidenceCaptureInput(replace(
        capture.evidence,
        source_ref="nz_parliament_hansard",
        external_key="nz_parliament_hansard:fixture-debate",
    ))
    plan = plan_evidence_refresh(
        _candidate(), _batch(), [unsupported], remaining_hydrations=25,
    )
    assert plan.status is EvidenceRefreshStatus.DEFERRED
    assert plan.reason == "source_refresh_unsupported:nz_parliament_hansard"


def test_fresh_official_feed_metadata_is_current_without_article_html_provenance():
    capture = _fresh_official_feed_capture()
    assert "source_capture" not in capture.source_metadata
    assert capture.source_metadata["publication"]["source"] == "rss-pubDate"
    plan = plan_evidence_refresh(
        _candidate(), _batch(), [capture], remaining_hydrations=0,
    )
    assert plan.status is EvidenceRefreshStatus.CURRENT
    assert plan.targets == ()


def test_fresh_official_event_witness_with_unknown_publication_is_current():
    capture = _fresh_official_event_capture()
    assert capture.evidence.published_at is None
    assert capture.source_metadata["event_date_provenance"]["source"] == "parliament-sittingDate"
    plan = plan_evidence_refresh(
        _candidate(), _batch(), [capture], remaining_hydrations=0,
    )
    assert plan.status is EvidenceRefreshStatus.CURRENT
    assert plan.targets == ()


@pytest.mark.parametrize("factory", [_fresh_official_feed_capture, _fresh_official_event_capture])
def test_legacy_official_capture_cannot_borrow_date_or_context_without_provenance(factory):
    capture = factory()
    legacy = EvidenceCaptureInput(capture.evidence)
    plan = plan_evidence_refresh(
        _candidate(), _batch(), [legacy], remaining_hydrations=25,
    )
    assert plan.status is EvidenceRefreshStatus.DEFERRED
    assert plan.reason == f"source_refresh_unsupported:{capture.evidence.source_ref}"


def test_an_official_event_witness_cannot_certify_a_legacy_modified_publication():
    capture = _fresh_official_event_capture()
    wrong_publication = EvidenceCaptureInput(
        replace(capture.evidence, published_at=LASTMOD),
        capture.source_metadata,
    )
    plan = plan_evidence_refresh(
        _candidate(), _batch(), [wrong_publication], remaining_hydrations=25,
    )
    assert plan.status is EvidenceRefreshStatus.DEFERRED


def test_capture_fingerprint_detects_date_or_metadata_changes():
    capture = _capture()
    changed_date = EvidenceCaptureInput(replace(capture.evidence, published_at=NOW))
    changed_metadata = EvidenceCaptureInput(capture.evidence, {"source_capture": {"version": 1}})
    assert capture_fingerprint([capture]) != capture_fingerprint([changed_date])
    assert capture_fingerprint([capture]) != capture_fingerprint([changed_metadata])


def _media_capture(evidence_id, source_key, url):
    return EvidenceCaptureInput(replace(
        _capture().evidence,
        id=evidence_id, source_ref=source_key, external_key=f"{source_key}:{url}",
        url=url, canonical_url=url, publisher_id=source_key, publisher=source_key,
        tier=CivicEvidenceTier.REPUTABLE_MEDIA, source_kind="independent-news",
        party_slug=None, stance=CivicEvidenceStance.CORROBORATING,
    ))


def test_an_outlet_whose_articles_cannot_be_captured_never_blocks_a_capturable_neighbour():
    odt = _media_capture(1, "odt", "https://www.odt.co.nz/news/national/plan-xu1c9lp9")
    stuff = _media_capture(2, "stuff", "https://www.stuff.co.nz/politics/361038702/plan")
    post = _media_capture(3, "the-post", "https://www.thepost.co.nz/politics/361081223/plan")

    plan = plan_evidence_refresh(
        _candidate(), _batch(), (odt, stuff, post), remaining_hydrations=25)
    assert plan.status is EvidenceRefreshStatus.PLANNED
    assert [target.evidence_id for target in plan.targets] == [1]

    alone = plan_evidence_refresh(_candidate(), _batch(), (stuff,), remaining_hydrations=25)
    assert alone.status is EvidenceRefreshStatus.CURRENT
    assert alone.targets == ()
