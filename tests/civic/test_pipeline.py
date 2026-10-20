# tests/civic/test_pipeline.py
"""The pure evidence engine, running against a real database.

`tests/civic/test_evidence_policy_fixtures.py` already proves the engine decides correctly; what
has to be proved here is that the *durable* half around it holds: that a decision and the
Candidate settlement it drives commit together, that a worker coming back to a Candidate replays
rather than re-decides, that the one bounded research attempt survives a crash without being spent
twice, and that a source item can never produce two Candidates.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from beehive.civic.clustering import (
    MAX_CLUSTERS_PER_RUN,
    CivicClusteringResult,
    CivicDocumentCluster,
    cluster_civic_documents,
)
from beehive.civic.documents import CivicDocument
from beehive.civic.evidence import (CivicCandidateClaim, CivicClaimCategory, CivicContentClass,
                                    CivicOwnershipRegistry, CivicPublisher, CivicPublisherKind,
                                    CivicQualificationOutcome, CivicRecordKind,
                                    CivicRecordOperation, CivicSourceKind)
from beehive.civic.pipeline import (CIVIC_ENGINE_VERSION, CivicPipelineError,
                                    begin_civic_research, evidence_item_for,
                                    ingest_civic_documents, qualification_input_hash,
                                    qualify_civic_candidate, resume_civic_research)
from beehive.civic.reasons import CivicPolicyReason
from beehive.db.civic_batches import (get_batch_for_run, list_sealed_candidate_ids,
                                      seal_batch_if_claimed)
from beehive.db.civic_batches import get_or_create_batch_if_claimed
from beehive.db.civic_candidates import (add_candidate_evidence, get_candidate,
                                         list_candidate_evidence, list_run_candidates,
                                         mark_candidate_verified)
from beehive.db.civic_consumed_items import (get_consuming_candidate_id, list_candidate_items,
                                             list_consumed_item_keys)
from beehive.db.civic_qualifications import (CivicQualificationDriftError,
                                             get_latest_qualification, list_qualifications)
from beehive.db.civic_receipts import record_delivery_receipt
from beehive.db.civic_runs import (claim_civic_run, complete_civic_run, enqueue_civic_run,
                                   get_civic_run)
from beehive.db.civic_verifications import (list_research_attempts, record_research_attempt,
                                            record_verification)
from beehive.db.civic_watches import create_civic_watch
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import (CivicCandidateStatus, CivicDeliveryChannel,
                                  CivicEvidenceStance, CivicEvidenceTier, CivicResearchOutcome,
                                  CivicRunStatus, CivicVerificationVerdict)

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)

REGISTRY = CivicOwnershipRegistry.build([
    CivicPublisher(publisher_id="beehive", name="Beehive", kind=CivicPublisherKind.OFFICIAL,
                   source_kind=CivicSourceKind.GOVERNMENT),
    CivicPublisher(publisher_id="greens", name="Green Party", kind=CivicPublisherKind.PARTY,
                   source_kind=CivicSourceKind.PARTY_PRIMARY, party_slug="green-party"),
    CivicPublisher(publisher_id="stuff", name="Stuff", kind=CivicPublisherKind.MEDIA,
                   source_kind=CivicSourceKind.INDEPENDENT_NEWS, ownership_group="stuff-ltd",
                   ownership_cluster="stuff-ltd"),
    CivicPublisher(publisher_id="nzherald", name="NZ Herald", kind=CivicPublisherKind.MEDIA,
                   source_kind=CivicSourceKind.INDEPENDENT_NEWS, ownership_group="nzme",
                   ownership_cluster="nzme"),
])


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def run(conn):
    """A claimed, processing Run with a BUILDING Batch: the state every pipeline call needs."""
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    enqueued = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, enqueued.id, T0, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, enqueued.id, lease.run.claim_token, T0)
    return type("Ctx", (), {
        "watch_id": watch_id, "id": enqueued.id, "token": lease.run.claim_token,
        "batch": batch})()


def doc(key, title, *, source="nz_beehive_releases", tier=CivicEvidenceTier.OFFICIAL,
        minutes=0, url=None, excerpt="An official statement."):
    return CivicDocument(
        source_key=source, external_key=key,
        canonical_url=url or f"https://example.test/{source}/{key}", title=title, tier=tier,
        publisher="Beehive", first_seen_at=T0 + timedelta(minutes=minutes),
        published_at=T0 + timedelta(minutes=minutes), excerpt=excerpt)


def claim_for(candidate, *, category=CivicClaimCategory.PARTY_TIMELINE_EVENT,
              record_kind=CivicRecordKind.TIMELINE, durable=True,
              content_class=CivicContentClass.FACTUAL, fingerprint=None):
    return CivicCandidateClaim(
        candidate_id=str(candidate.id), fingerprint=fingerprint or candidate.dedupe_key,
        record_kind=record_kind, operation=CivicRecordOperation.ADD, subject_id="green-party",
        record_key=candidate.dedupe_key, event_date=T0.date(), category=category,
        content_class=content_class, durable=durable, claim_text="A durable event")


def test_prepared_clustering_stays_authoritative_after_hydration_changes_date(conn, run):
    metadata_only = CivicDocument(
        source_key="green",
        external_key="party-release",
        canonical_url="https://greens.example/party-release",
        title="Green Party housing policy release",
        tier=CivicEvidenceTier.PARTY_PRIMARY,
        publisher="Green Party",
        first_seen_at=T0 + timedelta(days=2),
    )
    dated_official = doc(
        "official-release",
        "Parliament publishes a separate tax record",
        minutes=60,
    )
    initial = cluster_civic_documents(
        (metadata_only, dated_official),
        max_clusters=1,
    )
    assert initial.clusters[0].cluster_key == metadata_only.dedupe_key
    hydrated = replace(
        metadata_only,
        published_at=T0 - timedelta(days=30),
        excerpt="The Green Party published a detailed housing policy.",
    )
    prepared = CivicClusteringResult(
        clusters=(
            CivicDocumentCluster(
                cluster_key=initial.clusters[0].cluster_key,
                documents=(hydrated,),
            ),
        ),
        deferred_clusters=initial.deferred_clusters,
        skipped_item_keys=initial.skipped_item_keys,
    )

    result = ingest_civic_documents(
        conn,
        run_id=run.id,
        claim_token=run.token,
        documents=(hydrated, dated_official),
        now=T0,
        max_clusters=1,
        clustering=prepared,
    )

    assert [candidate.dedupe_key for candidate in result.created_candidates] == [
        metadata_only.dedupe_key
    ]
    assert list_consumed_item_keys(conn, run.watch_id) == [
        metadata_only.dedupe_key
    ]


def evidence(conn, candidate_id, key, *, publisher_id="beehive",
             source_kind=CivicSourceKind.GOVERNMENT, tier=CivicEvidenceTier.OFFICIAL,
             stance=CivicEvidenceStance.SUPPORTING, url=None, party_slug=None,
             subject_id="green-party", category=CivicClaimCategory.PARTY_TIMELINE_EVENT,
             accessible=True, paywalled=False, anonymous=False, now=T0, **overrides):
    return add_candidate_evidence(
        conn, candidate_id, "official_feed", key, url or f"https://example.test/{key}",
        "Release", tier, stance, now, publisher="Beehive",
        publisher_id=publisher_id,
        source_kind=source_kind.value if source_kind else None,
        party_slug=party_slug, subject_id=subject_id, claim_category=category.value,
        event_date=T0.date(), canonical_url=url or f"https://example.test/{key}",
        accessible_full_text=accessible, paywalled=paywalled,
        anonymous_source_dependency=anonymous, **overrides)


def qualify(conn, run, candidate, **kwargs):
    return qualify_civic_candidate(
        conn, run_id=run.id, claim_token=run.token, claim=claim_for(candidate, **kwargs),
        registry=REGISTRY, now=T0)


# ============================================================================
# Ingest: clustering, Candidates and consumed items in one transaction
# ============================================================================


def test_ingest_creates_one_candidate_per_development(conn, run):
    result = ingest_civic_documents(
        conn, run_id=run.id, claim_token=run.token, now=T0, documents=[
            doc("a", "Government announces new housing policy"),
            doc("b", "Government announces new housing policy today", source="nz_stuff",
                tier=CivicEvidenceTier.REPUTABLE_MEDIA),
            doc("c", "Electoral Commission publishes enrolment statistics",
                source="nz_electoral_commission"),
        ])
    assert len(result.created_candidates) == 2
    assert len(list_run_candidates(conn, run.id)) == 2


def test_ingest_claims_every_document_it_consumed(conn, run):
    result = ingest_civic_documents(
        conn, run_id=run.id, claim_token=run.token, now=T0, documents=[
            doc("a", "Government announces new housing policy"),
            doc("b", "Government announces new housing policy today", source="nz_stuff",
                tier=CivicEvidenceTier.REPUTABLE_MEDIA),
        ])
    candidate = result.created_candidates[0]
    assert {item.item_key for item in list_candidate_items(conn, candidate.id)} == {
        "nz_beehive_releases:a", "nz_stuff:b"}


def test_a_consumed_item_cannot_produce_a_second_candidate(conn, run):
    """The whole point of civic_consumed_items: re-ingesting the same article on a later Run
    must not cluster it into a brand new Candidate under a different key."""
    ingest_civic_documents(
        conn, run_id=run.id, claim_token=run.token, now=T0,
        documents=[doc("a", "Government announces new housing policy")])
    complete_civic_run(conn, run.id, run.token, CivicRunStatus.COMPLETED, T0)

    later = T0 + timedelta(days=1)
    second = enqueue_civic_run(conn, run.watch_id, later, later)
    lease = claim_civic_run(conn, second.id, later, 600, 3600)
    get_or_create_batch_if_claimed(conn, second.id, lease.run.claim_token, later)
    result = ingest_civic_documents(
        conn, run_id=second.id, claim_token=lease.run.claim_token, now=later,
        documents=[doc("a", "Government announces new housing policy")])
    assert result.created_candidates == ()
    assert result.skipped_item_keys == ("nz_beehive_releases:a",)
    assert len(list_run_candidates(conn, second.id)) == 0


def test_re_ingesting_within_one_run_is_idempotent(conn, run):
    documents = [doc("a", "Government announces new housing policy")]
    first = ingest_civic_documents(
        conn, run_id=run.id, claim_token=run.token, now=T0, documents=documents)
    second = ingest_civic_documents(
        conn, run_id=run.id, claim_token=run.token, now=T0, documents=documents)
    assert len(first.created_candidates) == 1
    assert second.created_candidates == ()
    assert second.skipped_item_keys == ("nz_beehive_releases:a",)
    assert len(list_run_candidates(conn, run.id)) == 1


def test_ingest_writes_nothing_when_the_claim_has_moved_on(conn, run):
    assert ingest_civic_documents(
        conn, run_id=run.id, claim_token="not-the-token", now=T0,
        documents=[doc("a", "Anything")]) is None
    assert list_run_candidates(conn, run.id) == []
    assert list_consumed_item_keys(conn, run.watch_id) == []


def test_ingest_writes_nothing_once_the_deadline_has_arrived(conn, run):
    """The deadline is sampled after the lock, so a worker that blocked past it writes nothing
    rather than stamping work onto a Run whose budget is gone."""
    deadline = get_civic_run(conn, run.id).deadline_at
    assert ingest_civic_documents(
        conn, run_id=run.id, claim_token=run.token, now=deadline,
        documents=[doc("a", "Anything")]) is None
    assert list_run_candidates(conn, run.id) == []


def test_ingest_refuses_a_sealed_batch(conn, run):
    seal_batch_if_claimed(conn, run.batch.id, run.id, run.token, T0)
    with pytest.raises(CivicPipelineError, match="not 'building'"):
        ingest_civic_documents(
            conn, run_id=run.id, claim_token=run.token, now=T0,
            documents=[doc("a", "Anything")])


def test_a_failed_ingest_leaves_no_partial_state(conn, run):
    """The Candidate and the claims on the items that produced it commit together or not at
    all."""
    with pytest.raises(ValueError, match="duplicate Civic document"):
        ingest_civic_documents(
            conn, run_id=run.id, claim_token=run.token, now=T0,
            documents=[doc("a", "One"), doc("a", "One")])
    assert list_run_candidates(conn, run.id) == []
    assert list_consumed_item_keys(conn, run.watch_id) == []


def test_ingest_reports_the_clusters_its_cap_held_back(conn, run):
    documents = [
        doc(f"k{index:02d}", f"{word} scheme {word} review {word}", minutes=index)
        for index, word in enumerate([
            "housing", "enrolment", "tariff", "fisheries", "broadband", "railway"])]
    result = ingest_civic_documents(
        conn, run_id=run.id, claim_token=run.token, now=T0, documents=documents,
        max_clusters=2)
    assert len(result.created_candidates) == 2
    assert len(result.deferred_clusters) == 4
    assert result.is_truncated
    assert MAX_CLUSTERS_PER_RUN == 25


# ============================================================================
# Qualification: decision, receipt and settlement in one transaction
# ============================================================================


def _one_candidate(conn, run, title="Government announces new housing policy"):
    result = ingest_civic_documents(
        conn, run_id=run.id, claim_token=run.token, now=T0, documents=[doc("a", title)])
    return result.created_candidates[0]


def test_a_qualified_candidate_waits_for_the_verifier(conn, run):
    """QUALIFIED never settles here. mark_candidate_verified is the only path to VERIFIED and it
    demands a supporting verdict, so publishing from the policy alone would skip verification."""
    candidate = _one_candidate(conn, run)
    evidence(conn, candidate.id, "e1")
    output = qualify(conn, run, candidate)
    assert output.record.outcome is CivicQualificationOutcome.QUALIFIED
    assert output.candidate.status is CivicCandidateStatus.VERIFYING
    assert output.candidate.settled_at is None


def test_a_screened_out_candidate_settles_as_rejected(conn, run):
    candidate = _one_candidate(conn, run)
    evidence(conn, candidate.id, "e1")
    output = qualify(conn, run, candidate, content_class=CivicContentClass.OPINION,
                     category=CivicClaimCategory.OPINION)
    assert output.record.outcome is CivicQualificationOutcome.SCREENED_OUT
    assert output.candidate.status is CivicCandidateStatus.REJECTED
    assert output.candidate.rejection_code == "screened_out"
    assert output.candidate.settled_at == T0


def test_an_out_of_scope_record_kind_settles_as_deferred_unsupported(conn, run):
    candidate = _one_candidate(conn, run)
    evidence(conn, candidate.id, "e1")
    output = qualify(conn, run, candidate, record_kind=CivicRecordKind.PARTY_REGISTRATION)
    assert output.record.outcome is CivicQualificationOutcome.DEFERRED_UNSUPPORTED
    assert output.candidate.rejection_code == "deferred_unsupported"


def test_an_evidence_deferral_waits_for_its_research_pass(conn, run):
    candidate = _one_candidate(conn, run)
    evidence(conn, candidate.id, "m1", publisher_id="stuff",
             source_kind=CivicSourceKind.INDEPENDENT_NEWS,
             tier=CivicEvidenceTier.REPUTABLE_MEDIA)
    output = qualify(conn, run, candidate)
    assert output.record.outcome is CivicQualificationOutcome.DEFERRED_EVIDENCE
    assert output.candidate.status is CivicCandidateStatus.RESEARCHING
    assert output.candidate.settled_at is None


def test_a_duplicate_settles_with_the_dedup_reason_as_its_code(conn, run):
    first = _one_candidate(conn, run)
    evidence(conn, first.id, "e1")
    qualify(conn, run, first)
    record_verification(conn, first.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", T0)
    mark_candidate_verified(conn, first.id, T0)
    seal_batch_if_claimed(conn, run.batch.id, run.id, run.token, T0)
    complete_civic_run(conn, run.id, run.token, CivicRunStatus.COMPLETED, T0)

    later = T0 + timedelta(days=1)
    second_run = enqueue_civic_run(conn, run.watch_id, later, later)
    lease = claim_civic_run(conn, second_run.id, later, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, second_run.id, lease.run.claim_token, later)
    result = ingest_civic_documents(
        conn, run_id=second_run.id, claim_token=lease.run.claim_token, now=later,
        documents=[doc("b", "Something else entirely different")])
    repeat = result.created_candidates[0]
    # A brand new fingerprint, but citing the article the first Candidate already published on.
    evidence(conn, repeat.id, "e2", url="https://example.test/e1", now=later)
    output = qualify_civic_candidate(
        conn, run_id=second_run.id, claim_token=lease.run.claim_token,
        claim=claim_for(repeat), registry=REGISTRY, now=later)
    assert output.record.outcome is CivicQualificationOutcome.DUPLICATE
    assert output.candidate.rejection_code == (
        CivicPolicyReason.DUPLICATE_CANONICAL_URL.value)
    assert output.record.matched_candidate_id == first.id
    assert batch is not None


def test_qualification_writes_nothing_when_the_claim_has_moved_on(conn, run):
    candidate = _one_candidate(conn, run)
    evidence(conn, candidate.id, "e1")
    assert qualify_civic_candidate(
        conn, run_id=run.id, claim_token="not-the-token", claim=claim_for(candidate),
        registry=REGISTRY, now=T0) is None
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.DRAFTED
    assert list_qualifications(conn, candidate.id) == []


def test_a_claim_for_the_wrong_candidate_is_an_explicit_error(conn, run):
    candidate = _one_candidate(conn, run)
    evidence(conn, candidate.id, "e1")
    with pytest.raises(CivicPipelineError, match="is not the Candidate's dedupe_key"):
        qualify(conn, run, candidate, fingerprint="somebody-elses-key")


def test_evidence_without_policy_fields_is_an_explicit_error(conn, run):
    """A row with no publisher cannot be judged. Treating it as anonymous material would make a
    bookkeeping bug look like an ownership failure."""
    candidate = _one_candidate(conn, run)
    add_candidate_evidence(
        conn, candidate.id, "official_feed", "e1", "https://example.test/e1", "Release",
        CivicEvidenceTier.OFFICIAL, CivicEvidenceStance.SUPPORTING, T0)
    with pytest.raises(CivicPipelineError, match="has no publisher_id"):
        qualify(conn, run, candidate)


# ============================================================================
# Replay and drift
# ============================================================================


def test_qualifying_twice_replays_the_stored_decision(conn, run):
    candidate = _one_candidate(conn, run)
    evidence(conn, candidate.id, "e1")
    first = qualify(conn, run, candidate)
    second = qualify_civic_candidate(
        conn, run_id=run.id, claim_token=run.token, claim=claim_for(candidate),
        registry=REGISTRY, now=T0 + timedelta(minutes=20))
    assert not first.replayed
    assert second.replayed
    assert second.record.id == first.record.id
    assert second.record.decided_at == T0
    assert len(list_qualifications(conn, candidate.id)) == 1


def test_new_evidence_arriving_before_a_replay_is_detected_as_drift(conn, run):
    """A resumed Run cannot change a decision it has already recorded without saying so."""
    candidate = _one_candidate(conn, run)
    evidence(conn, candidate.id, "m1", publisher_id="stuff",
             source_kind=CivicSourceKind.INDEPENDENT_NEWS,
             tier=CivicEvidenceTier.REPUTABLE_MEDIA)
    qualify(conn, run, candidate)
    evidence(conn, candidate.id, "e1")
    with pytest.raises(CivicQualificationDriftError):
        qualify(conn, run, candidate)


def test_the_input_hash_does_not_depend_on_evidence_order(conn, run):
    candidate = _one_candidate(conn, run)
    evidence(conn, candidate.id, "e1")
    evidence(conn, candidate.id, "e2", url="https://example.test/e2")
    items = [evidence_item_for(row) for row in list_candidate_evidence(conn, candidate.id)]
    forward = qualification_input_hash(
        claim_for(candidate), items, registry=REGISTRY, dedup_fingerprints=[], dedup_urls=[])
    backward = qualification_input_hash(
        claim_for(candidate), list(reversed(items)), registry=REGISTRY, dedup_fingerprints=[],
        dedup_urls=[])
    assert forward == backward


def test_the_input_hash_does_not_depend_on_the_wall_clock(conn, run):
    """A worker resuming later is replaying, not drifting: folding `now` into the hash would make
    every resume look like a changed decision, and a recovered Run keeps its original deadline so
    the resumed worker really is the same claim coming back."""
    candidate = _one_candidate(conn, run)
    evidence(conn, candidate.id, "e1")
    first = qualify(conn, run, candidate)
    second = qualify_civic_candidate(
        conn, run_id=run.id, claim_token=run.token, claim=claim_for(candidate),
        registry=REGISTRY, now=T0 + timedelta(minutes=50))
    assert second.record.input_hash == first.record.input_hash
    assert second.replayed


def test_a_changed_registry_is_drift(conn, run):
    candidate = _one_candidate(conn, run)
    evidence(conn, candidate.id, "e1")
    qualify(conn, run, candidate)
    thinner = CivicOwnershipRegistry.build([
        CivicPublisher(publisher_id="beehive", name="Beehive",
                       kind=CivicPublisherKind.OFFICIAL,
                       source_kind=CivicSourceKind.GOVERNMENT),
    ])
    with pytest.raises(CivicQualificationDriftError):
        qualify_civic_candidate(
            conn, run_id=run.id, claim_token=run.token, claim=claim_for(candidate),
            registry=thinner, now=T0)


# ============================================================================
# The one bounded research attempt
# ============================================================================


def _deferred_candidate(conn, run):
    candidate = _one_candidate(conn, run)
    evidence(conn, candidate.id, "m1", publisher_id="stuff",
             source_kind=CivicSourceKind.INDEPENDENT_NEWS,
             tier=CivicEvidenceTier.REPUTABLE_MEDIA)
    qualify(conn, run, candidate)
    return candidate


def test_research_resolves_when_the_extra_evidence_closes_the_gap(conn, run):
    candidate = _deferred_candidate(conn, run)
    attempt = begin_civic_research(
        conn, run_id=run.id, claim_token=run.token, candidate_id=candidate.id, now=T0)
    assert attempt == 1
    evidence(conn, candidate.id, "m2", publisher_id="nzherald",
             source_kind=CivicSourceKind.INDEPENDENT_NEWS,
             tier=CivicEvidenceTier.REPUTABLE_MEDIA, url="https://example.test/m2")
    record_research_attempt(
        conn, candidate.id, attempt, CivicResearchOutcome.RESOLVED, T0,
        T0 + timedelta(minutes=1), evidence_added=1)
    output = qualify(conn, run, candidate)
    assert output.record.outcome is CivicQualificationOutcome.QUALIFIED
    assert CivicPolicyReason.RESEARCH_RETRY_RESOLVED in output.record.reasons
    assert output.record.research_attempts_used == 1
    assert output.candidate.status is CivicCandidateStatus.VERIFYING


def test_research_that_finds_nothing_exhausts_the_budget_and_settles(conn, run):
    candidate = _deferred_candidate(conn, run)
    attempt = begin_civic_research(
        conn, run_id=run.id, claim_token=run.token, candidate_id=candidate.id, now=T0)
    evidence(conn, candidate.id, "c1", publisher_id="stuff",
             source_kind=CivicSourceKind.INDEPENDENT_NEWS,
             tier=CivicEvidenceTier.REPUTABLE_MEDIA, url="https://example.test/c1",
             stance=CivicEvidenceStance.CONTEXT)
    record_research_attempt(
        conn, candidate.id, attempt, CivicResearchOutcome.UNRESOLVED, T0,
        T0 + timedelta(minutes=1))
    output = qualify(conn, run, candidate)
    assert output.record.outcome is CivicQualificationOutcome.DEFERRED_EVIDENCE
    assert CivicPolicyReason.RESEARCH_RETRY_EXHAUSTED in output.record.reasons
    assert output.candidate.status is CivicCandidateStatus.REJECTED
    assert output.candidate.rejection_code == "deferred_evidence"


def test_the_attempt_can_only_be_spent_once(conn, run):
    candidate = _deferred_candidate(conn, run)
    first = begin_civic_research(
        conn, run_id=run.id, claim_token=run.token, candidate_id=candidate.id, now=T0)
    second = begin_civic_research(
        conn, run_id=run.id, claim_token=run.token, candidate_id=candidate.id, now=T0)
    assert first == 1
    assert second is None


def test_a_decision_research_cannot_change_never_spends_the_attempt(conn, run):
    """A screened-out Candidate is decided by its subject matter. Spending the single attempt on
    it would burn the budget a real evidence gap needs."""
    candidate = _one_candidate(conn, run)
    evidence(conn, candidate.id, "e1")
    qualify(conn, run, candidate, content_class=CivicContentClass.OPINION,
            category=CivicClaimCategory.OPINION)
    assert begin_civic_research(
        conn, run_id=run.id, claim_token=run.token, candidate_id=candidate.id, now=T0) is None
    assert get_candidate(conn, candidate.id).research_attempt_count == 0


def test_research_before_any_decision_is_an_explicit_error(conn, run):
    candidate = _one_candidate(conn, run)
    with pytest.raises(CivicPipelineError, match="has no qualification on record"):
        begin_civic_research(
            conn, run_id=run.id, claim_token=run.token, candidate_id=candidate.id, now=T0)


def test_a_crash_after_reserving_leaves_the_attempt_spent_and_findable(conn, run):
    """The spend commits before any research runs, so a worker that dies mid-pass costs the
    attempt. Its replacement has to be able to tell that from having a pass left."""
    candidate = _deferred_candidate(conn, run)
    attempt = begin_civic_research(
        conn, run_id=run.id, claim_token=run.token, candidate_id=candidate.id, now=T0)
    # The worker dies here: reserved, nothing recorded.
    assert get_candidate(conn, candidate.id).research_attempt_count == 1
    assert list_research_attempts(conn, candidate.id) == []
    assert resume_civic_research(conn, candidate.id) == attempt
    assert begin_civic_research(
        conn, run_id=run.id, claim_token=run.token, candidate_id=candidate.id, now=T0) is None


def test_qualifying_on_an_unrecorded_research_pass_is_refused(conn, run):
    candidate = _deferred_candidate(conn, run)
    begin_civic_research(
        conn, run_id=run.id, claim_token=run.token, candidate_id=candidate.id, now=T0)
    with pytest.raises(CivicPipelineError, match="outcome was never recorded"):
        qualify(conn, run, candidate)


def test_the_resumed_worker_settles_the_reserved_attempt_and_carries_on(conn, run):
    candidate = _deferred_candidate(conn, run)
    attempt = begin_civic_research(
        conn, run_id=run.id, claim_token=run.token, candidate_id=candidate.id, now=T0)
    pending = resume_civic_research(conn, candidate.id)
    record_research_attempt(
        conn, candidate.id, pending, CivicResearchOutcome.ERROR, T0, T0 + timedelta(minutes=1),
        error_code="worker_crashed")
    assert resume_civic_research(conn, candidate.id) is None
    output = qualify(conn, run, candidate)
    assert output.record.research_attempts_used == attempt
    assert output.record.outcome is CivicQualificationOutcome.DEFERRED_EVIDENCE
    assert output.candidate.status is CivicCandidateStatus.REJECTED


def test_a_lost_claim_never_spends_the_attempt(conn, run):
    candidate = _deferred_candidate(conn, run)
    assert begin_civic_research(
        conn, run_id=run.id, claim_token="not-the-token", candidate_id=candidate.id,
        now=T0) is None
    assert get_candidate(conn, candidate.id).research_attempt_count == 0


# ============================================================================
# Batch interaction
# ============================================================================


def test_only_verified_candidates_reach_the_envelope(conn, run):
    result = ingest_civic_documents(
        conn, run_id=run.id, claim_token=run.token, now=T0, documents=[
            doc("a", "Housing scheme housing review housing"),
            doc("b", "Enrolment scheme enrolment review enrolment",
                source="nz_electoral_commission", minutes=5),
        ])
    good, bad = sorted(result.created_candidates, key=lambda candidate: candidate.dedupe_key)
    evidence(conn, good.id, "e1")
    evidence(conn, bad.id, "e2", url="https://example.test/e2")
    qualify(conn, run, good)
    qualify(conn, run, bad, content_class=CivicContentClass.POLL,
            category=CivicClaimCategory.POLL)
    record_verification(conn, good.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", T0)
    mark_candidate_verified(conn, good.id, T0)

    sealed = seal_batch_if_claimed(conn, run.batch.id, run.id, run.token, T0)
    assert sealed.candidate_count == 1
    assert list_sealed_candidate_ids(conn, sealed.id) == [good.id]


def test_a_qualified_candidate_still_blocks_the_seal_until_it_is_verified(conn, run):
    """Qualification is not publication. A Candidate the policy allowed is still VERIFYING, and
    the seal refuses an unsettled Batch by design."""
    candidate = _one_candidate(conn, run)
    evidence(conn, candidate.id, "e1")
    qualify(conn, run, candidate)
    with pytest.raises(ValueError, match="still unsettled"):
        seal_batch_if_claimed(conn, run.batch.id, run.id, run.token, T0)


def test_a_run_whose_candidates_all_settled_seals_empty(conn, run):
    candidate = _one_candidate(conn, run)
    evidence(conn, candidate.id, "e1")
    qualify(conn, run, candidate, content_class=CivicContentClass.OPINION,
            category=CivicClaimCategory.OPINION)
    sealed = seal_batch_if_claimed(conn, run.batch.id, run.id, run.token, T0)
    assert sealed.candidate_count == 0


def test_a_receipted_fingerprint_suppresses_it_for_fourteen_days(conn, run):
    candidate = _one_candidate(conn, run)
    evidence(conn, candidate.id, "e1")
    qualify(conn, run, candidate)
    record_verification(conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", T0)
    mark_candidate_verified(conn, candidate.id, T0)
    sealed = seal_batch_if_claimed(conn, run.batch.id, run.id, run.token, T0)
    from beehive.db.civic_batches import bind_batch_payload_hash, claim_batch_for_delivery
    sealed = bind_batch_payload_hash(conn, sealed.id, "f" * 64, T0)
    delivery = claim_batch_for_delivery(conn, sealed.id, T0, 60)
    record_delivery_receipt(
        conn, sealed.id, delivery.batch.claim_token, CivicDeliveryChannel.SPOOL, "/spool", T0)
    complete_civic_run(conn, run.id, run.token, CivicRunStatus.COMPLETED, T0)

    later = T0 + timedelta(days=10)
    second_run = enqueue_civic_run(conn, run.watch_id, later, later)
    lease = claim_civic_run(conn, second_run.id, later, 600, 3600)
    get_or_create_batch_if_claimed(conn, second_run.id, lease.run.claim_token, later)
    result = ingest_civic_documents(
        conn, run_id=second_run.id, claim_token=lease.run.claim_token, now=later,
        documents=[doc("z", "Quite unrelated wording entirely")])
    repeat = result.created_candidates[0]
    evidence(conn, repeat.id, "z1", url="https://example.test/z1", now=later)
    output = qualify_civic_candidate(
        conn, run_id=second_run.id, claim_token=lease.run.claim_token,
        claim=claim_for(repeat, fingerprint=repeat.dedupe_key), registry=REGISTRY, now=later)
    # A different fingerprint and a different URL: not a duplicate, which is what makes the
    # positive published-window case in tests/db/test_civic_dedup.py meaningful.
    assert output.record.outcome is CivicQualificationOutcome.QUALIFIED
    assert get_batch_for_run(conn, second_run.id) is not None


def test_the_engine_version_is_recorded_on_every_decision(conn, run):
    candidate = _one_candidate(conn, run)
    evidence(conn, candidate.id, "e1")
    output = qualify(conn, run, candidate)
    assert output.record.engine_version == CIVIC_ENGINE_VERSION
    assert get_latest_qualification(conn, candidate.id).engine_version == CIVIC_ENGINE_VERSION


# ============================================================================
# Capacity carryover
# ============================================================================


def _verified_via_pipeline(conn, run, key, title):
    result = ingest_civic_documents(
        conn, run_id=run.id, claim_token=run.token, now=T0,
        documents=[doc(key, title)])
    candidate = result.created_candidates[0]
    evidence(conn, candidate.id, f"e-{key}", url=f"https://example.test/e-{key}")
    qualify(conn, run, candidate)
    record_verification(conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", T0)
    return mark_candidate_verified(conn, candidate.id, T0)


def test_the_cap_carries_overflow_without_touching_its_receipts(conn, run):
    """Capacity is decided by the seal, never by the evidence policy: the carried Candidate's
    qualification receipt still says QUALIFIED, because nothing about its evidence changed."""
    words = ["housing", "enrolment", "tariff", "fisheries", "broadband", "railway", "curriculum"]
    candidates = [
        _verified_via_pipeline(conn, run, f"k{index}", f"{word} scheme {word} review {word}")
        for index, word in enumerate(words)]
    sealed = seal_batch_if_claimed(conn, run.batch.id, run.id, run.token, T0)

    assert sealed.candidate_count == 6
    carried = candidates[6]
    assert get_candidate(conn, carried.id).status is CivicCandidateStatus.DEFERRED_CAPACITY
    record = get_latest_qualification(conn, carried.id)
    assert record.outcome is CivicQualificationOutcome.QUALIFIED
    assert record.record_kind is CivicRecordKind.TIMELINE
    assert len(list_qualifications(conn, carried.id)) == 1


def test_an_empty_ingest_still_adopts_yesterdays_overflow(conn, run):
    words = ["housing", "enrolment", "tariff", "fisheries", "broadband", "railway", "curriculum"]
    candidates = [
        _verified_via_pipeline(conn, run, f"k{index}", f"{word} scheme {word} review {word}")
        for index, word in enumerate(words)]
    seal_batch_if_claimed(conn, run.batch.id, run.id, run.token, T0)
    complete_civic_run(conn, run.id, run.token, CivicRunStatus.COMPLETED, T0)

    later = T0 + timedelta(days=1)
    second = enqueue_civic_run(conn, run.watch_id, later, later)
    lease = claim_civic_run(conn, second.id, later, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, second.id, lease.run.claim_token, later)
    result = ingest_civic_documents(
        conn, run_id=second.id, claim_token=lease.run.claim_token, now=later, documents=[])

    assert result.created_candidates == ()
    assert result.adopted_candidate_ids == (candidates[6].id,)
    adopted = get_candidate(conn, candidates[6].id)
    assert adopted.status is CivicCandidateStatus.VERIFIED
    assert adopted.batch_id == batch.id
    sealed = seal_batch_if_claimed(conn, batch.id, second.id, lease.run.claim_token, later)
    assert sealed.candidate_count == 1


def test_an_adopted_candidate_is_never_requalified(conn, run):
    """Adoption moves run/batch membership and nothing else, so the decision that let it publish
    is the one that was taken the day it was built."""
    words = ["housing", "enrolment", "tariff", "fisheries", "broadband", "railway", "curriculum"]
    candidates = [
        _verified_via_pipeline(conn, run, f"k{index}", f"{word} scheme {word} review {word}")
        for index, word in enumerate(words)]
    seal_batch_if_claimed(conn, run.batch.id, run.id, run.token, T0)
    complete_civic_run(conn, run.id, run.token, CivicRunStatus.COMPLETED, T0)
    before = get_latest_qualification(conn, candidates[6].id)

    later = T0 + timedelta(days=1)
    second = enqueue_civic_run(conn, run.watch_id, later, later)
    lease = claim_civic_run(conn, second.id, later, 600, 3600)
    get_or_create_batch_if_claimed(conn, second.id, lease.run.claim_token, later)
    ingest_civic_documents(
        conn, run_id=second.id, claim_token=lease.run.claim_token, now=later, documents=[])

    after = get_latest_qualification(conn, candidates[6].id)
    assert after.id == before.id
    assert after.run_id == before.run_id, "the receipt still names the Run that decided it"
    assert after.decided_at == before.decided_at
    assert after.input_hash == before.input_hash


def test_a_carried_candidate_keeps_owning_its_source_items(conn, run):
    words = ["housing", "enrolment", "tariff", "fisheries", "broadband", "railway", "curriculum"]
    candidates = [
        _verified_via_pipeline(conn, run, f"k{index}", f"{word} scheme {word} review {word}")
        for index, word in enumerate(words)]
    seal_batch_if_claimed(conn, run.batch.id, run.id, run.token, T0)
    complete_civic_run(conn, run.id, run.token, CivicRunStatus.COMPLETED, T0)
    carried = candidates[6]

    assert [item.item_key for item in list_candidate_items(conn, carried.id)] == [
        "nz_beehive_releases:k6"]
    # And the item stays consumed, so re-reading the same article tomorrow cannot mint a rival.
    later = T0 + timedelta(days=1)
    second = enqueue_civic_run(conn, run.watch_id, later, later)
    lease = claim_civic_run(conn, second.id, later, 600, 3600)
    get_or_create_batch_if_claimed(conn, second.id, lease.run.claim_token, later)
    result = ingest_civic_documents(
        conn, run_id=second.id, claim_token=lease.run.claim_token, now=later,
        documents=[doc("k6", "curriculum scheme curriculum review curriculum")])
    assert result.created_candidates == ()
    assert result.skipped_item_keys == ("nz_beehive_releases:k6",)
    assert get_consuming_candidate_id(conn, run.watch_id, "nz_beehive_releases:k6") == carried.id


def test_capacity_is_never_recorded_as_an_evidence_outcome(conn, run):
    """The evidence policy has no business deciding capacity, and settling a Candidate for it
    would make it terminal -- unable to come back."""
    candidate = _one_candidate(conn, run)
    evidence(conn, candidate.id, "e1")
    output = qualify(conn, run, candidate)
    assert output.record.outcome is not CivicQualificationOutcome.DEFERRED_CAPACITY
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM civic_qualifications WHERE outcome = 'deferred_capacity'"
    ).fetchone()["n"] == 0
