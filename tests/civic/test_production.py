from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from beehive.civic.clustering import CivicDocumentCluster
from beehive.civic.documents import CivicDocument
from beehive.civic.production import (
    PartySubjectResolutionStatus,
    build_ownership_registry,
    build_source_profiles,
    construct_candidate_claim,
    event_date_for_cluster,
    persist_cluster_evidence,
    persist_research_evidence,
    resolve_cluster_party_subject,
)
from beehive.civic.source_extraction import PublicationWitness
from beehive.db.civic_batches import get_or_create_batch_if_claimed
from beehive.db.civic_candidates import add_candidate_evidence, list_candidate_evidence
from beehive.db.civic_evidence_captures import list_evidence_capture_inputs
from beehive.db.civic_runs import claim_civic_run, enqueue_civic_run
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import CivicCandidateStatus
from beehive.domain.civic import CivicEvidenceStance, CivicEvidenceTier
from tests.civic.civic_worker_support import (
    SOURCE_ENTRY,
    T0,
    create_partymap_watch,
    housing_document,
    with_official_feed_metadata,
)
from beehive.civic.pipeline import ingest_civic_documents


@pytest.mark.parametrize("research", [False, True])
def test_each_evidence_row_keeps_its_own_date_instead_of_candidate_context(tmp_path, research):
    conn = connect(str(tmp_path / "source-local-dates.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn)
    run = enqueue_civic_run(conn, watch.id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 300, 1200)
    get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, T0)
    later = with_official_feed_metadata(replace(
        housing_document(), title="Green Party housing hearing update",
        excerpt="The Green Party provided an update on housing hearings."))
    earlier = with_official_feed_metadata(replace(
        later, external_key="earlier-update",
        canonical_url="https://www.beehive.govt.nz/release/earlier-update",
        published_at=T0 - timedelta(days=1)))
    ingested = ingest_civic_documents(
        conn, run_id=run.id, claim_token=lease.run.claim_token,
        documents=(earlier,), now=T0)
    candidate = ingested.created_candidates[0]
    cluster = CivicDocumentCluster(
        candidate.dedupe_key, (earlier,) if research else (earlier, later))
    persist_cluster_evidence(
        conn, run_id=run.id, claim_token=lease.run.claim_token, candidate=candidate,
        cluster=cluster, watch=watch, subject_id="green",
        profiles=build_source_profiles(), now=T0)
    if research:
        persist_research_evidence(
            conn, run_id=run.id, claim_token=lease.run.claim_token, candidate=candidate,
            claim=construct_candidate_claim(conn, candidate), documents=(later,),
            profiles=build_source_profiles(), now=T0)
    dates = {
        evidence.external_key: evidence.event_date
        for evidence in list_candidate_evidence(conn, candidate.id)
    }
    assert dates[earlier.dedupe_key] == date(2026, 7, 31)
    assert dates[later.dedupe_key] == date(2026, 8, 1)
    conn.close()


def test_registry_comes_from_application_catalogs():
    registry = build_ownership_registry()
    profiles = build_source_profiles()
    assert registry.get("nz_beehive_releases").source_kind.value == "government"
    assert registry.get("green").party_slug == "green"
    assert registry.get("stuff").ownership_group == profiles["stuff"].ownership_group
    assert registry.get("stuff").ownership_cluster == profiles["stuff"].ownership_cluster


def test_party_primary_profile_is_authoritative_over_remote_text():
    document = CivicDocument(
        source_key="green",
        external_key="release",
        canonical_url="https://www.greens.org.nz/release",
        title="Response to the National Party",
        tier=CivicEvidenceTier.PARTY_PRIMARY,
        publisher="Green Party of Aotearoa New Zealand",
        first_seen_at=T0,
        excerpt="The National Party is discussed in this Green Party release.",
    )
    resolution = resolve_cluster_party_subject(
        CivicDocumentCluster(document.dedupe_key, (document,)),
        build_source_profiles(),
    )
    assert resolution.status is PartySubjectResolutionStatus.RESOLVED
    assert resolution.subject_id == "green"


def test_official_party_aliases_require_safe_phrases_not_generic_tokens():
    document = CivicDocument(
        source_key="nz_beehive_releases",
        external_key="generic-words",
        canonical_url="https://www.beehive.govt.nz/release/generic-words",
        title="National housing work includes a green investment fund",
        tier=CivicEvidenceTier.OFFICIAL,
        publisher="New Zealand Government (Beehive)",
        first_seen_at=T0,
        excerpt="The act creates an opportunity for a conservative estimate.",
    )
    resolution = resolve_cluster_party_subject(
        CivicDocumentCluster(document.dedupe_key, (document,)),
        build_source_profiles(),
    )
    assert resolution.status is PartySubjectResolutionStatus.UNSUPPORTED
    assert resolution.subject_id is None


def test_ordinary_top_party_and_free_palestine_prose_resolves_to_no_party():
    document = CivicDocument(
        source_key="nz_beehive_releases",
        external_key="ordinary-prose",
        canonical_url="https://www.beehive.govt.nz/release/ordinary-prose",
        title="Poll asks which top party leads on housing",
        tier=CivicEvidenceTier.OFFICIAL,
        publisher="New Zealand Government (Beehive)",
        first_seen_at=T0,
        excerpt="Protesters called on ministers to free Palestine.",
    )
    resolution = resolve_cluster_party_subject(
        CivicDocumentCluster(document.dedupe_key, (document,)),
        build_source_profiles(),
    )
    assert resolution.status is PartySubjectResolutionStatus.UNSUPPORTED
    assert resolution.subject_id is None


@pytest.mark.parametrize(
    ("phrase", "slug"),
    [
        ("Opportunity Party", "opportunity"),
        ("Free Palestine Party", "free-palestine"),
    ],
)
def test_current_context_specific_party_names_resolve(phrase, slug):
    document = CivicDocument(
        source_key="nz_beehive_releases",
        external_key=slug,
        canonical_url=f"https://www.beehive.govt.nz/release/{slug}",
        title=f"{phrase} housing announcement",
        tier=CivicEvidenceTier.OFFICIAL,
        publisher="New Zealand Government (Beehive)",
        first_seen_at=T0,
    )
    resolution = resolve_cluster_party_subject(
        CivicDocumentCluster(document.dedupe_key, (document,)),
        build_source_profiles(),
    )
    assert resolution.status is PartySubjectResolutionStatus.RESOLVED
    assert resolution.subject_id == slug


def test_document_maps_to_persisted_evidence_and_claim(tmp_path):
    conn = connect(str(tmp_path / "mapping.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn)
    run = enqueue_civic_run(conn, watch.id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 60, 600)
    get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, T0)
    document = housing_document()
    ingest = ingest_civic_documents(
        conn,
        run_id=run.id,
        claim_token=lease.run.claim_token,
        documents=[document],
        now=T0,
    )
    candidate = ingest.created_candidates[0]
    cluster = CivicDocumentCluster(
        cluster_key=candidate.dedupe_key, documents=(document,))
    profiles = build_source_profiles()
    resolution = resolve_cluster_party_subject(cluster, profiles)
    persisted = persist_cluster_evidence(
        conn,
        run_id=run.id,
        claim_token=lease.run.claim_token,
        candidate=candidate,
        cluster=cluster,
        watch=watch,
        subject_id=resolution.subject_id,
        profiles=profiles,
        now=T0,
    )
    evidence = persisted[0]
    assert evidence.external_key == document.dedupe_key
    assert evidence.canonical_url == document.canonical_url
    assert evidence.publisher_id == SOURCE_ENTRY.source_key
    assert evidence.source_kind == "government"
    assert evidence.accessible_full_text
    assert not evidence.paywalled
    assert not evidence.anonymous_source_dependency

    claim = construct_candidate_claim(conn, candidate)
    assert claim.record_kind.value == "timeline"
    assert claim.operation.value == "add"
    assert claim.subject_id == "green"
    assert claim.category.value == "party-timeline-event"


def test_evidence_mapping_is_claim_fenced(tmp_path):
    conn = connect(str(tmp_path / "fence.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn)
    run = enqueue_civic_run(conn, watch.id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 60, 600)
    get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, T0)
    document = housing_document()
    ingest = ingest_civic_documents(
        conn,
        run_id=run.id,
        claim_token=lease.run.claim_token,
        documents=[document],
        now=T0,
    )
    candidate = ingest.created_candidates[0]
    cluster = CivicDocumentCluster(candidate.dedupe_key, (document,))
    profiles = build_source_profiles()
    resolution = resolve_cluster_party_subject(cluster, profiles)
    assert persist_cluster_evidence(
        conn,
        run_id=run.id,
        claim_token="lost",
        candidate=candidate,
        cluster=cluster,
        watch=watch,
        subject_id=resolution.subject_id,
        profiles=profiles,
        now=T0,
    ) is None
    assert list_candidate_evidence(conn, candidate.id) == []
    assert candidate.status is CivicCandidateStatus.DRAFTED


@pytest.mark.parametrize("research", [False, True], ids=["cluster", "research"])
def test_sitemap_modification_is_never_persisted_as_publication(tmp_path, research):
    conn = connect(str(tmp_path / "source-dates.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn)
    run = enqueue_civic_run(conn, watch.id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 60, 600)
    get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, T0)
    document = replace(
        housing_document(),
        source_key="national",
        external_key="https://www.national.org.nz/news/260903-paul-goldsmith-1",
        canonical_url="https://www.national.org.nz/news/260903-paul-goldsmith-1",
        tier=CivicEvidenceTier.PARTY_PRIMARY,
        publisher="New Zealand National Party",
        published_at=None,
        updated_at=datetime.fromisoformat("2026-09-04T08:41:29.788+00:00"),
        raw_metadata={
            "sitemap": "https://www.national.org.nz/sitemap.xml",
            "fixture_source_role": "modification-only",
        },
    )
    candidate = ingest_civic_documents(
        conn,
        run_id=run.id,
        claim_token=lease.run.claim_token,
        documents=[document],
        now=T0,
    ).created_candidates[0]
    common = dict(
        conn=conn,
        run_id=run.id,
        claim_token=lease.run.claim_token,
        candidate=candidate,
        profiles=build_source_profiles(),
        now=T0,
    )
    if research:
        persist_cluster_evidence(
            **common,
            cluster=CivicDocumentCluster(
                candidate.dedupe_key,
                (replace(document, external_key="dated-basis", published_at=T0),),
            ),
            watch=watch,
            subject_id="national",
        )
        evidence = persist_research_evidence(
            **common,
            claim=construct_candidate_claim(conn, candidate),
            documents=[document],
        )
    else:
        evidence = persist_cluster_evidence(
            **common,
            cluster=CivicDocumentCluster(candidate.dedupe_key, (document,)),
            watch=watch,
            subject_id="national",
        )
    assert evidence is not None
    assert evidence[0].published_at is None
    assert list_candidate_evidence(conn, candidate.id)[-1].published_at is None
    assert list_evidence_capture_inputs(conn, candidate.id)[-1].source_metadata == (
        document.raw_metadata)


def test_event_date_ignores_undated_corroboration_instead_of_borrowing_lastmod(tmp_path):
    conn = connect(str(tmp_path / "cluster-dates.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn)
    dated = with_official_feed_metadata(replace(
        housing_document(),
        published_at=datetime.fromisoformat("2026-09-02T23:58:38.731+00:00"),
    ))
    undated = replace(
        dated,
        external_key="undated-corroboration",
        published_at=None,
        updated_at=datetime.fromisoformat("2026-08-01T12:00:00+00:00"),
        raw_metadata={},
    )
    cluster = CivicDocumentCluster(dated.dedupe_key, (undated, dated))
    assert event_date_for_cluster(cluster, watch) == date(2026, 9, 3)
    assert event_date_for_cluster(
        CivicDocumentCluster(undated.dedupe_key, (undated,)), watch,
    ) is None


def test_official_sitting_event_date_is_independent_of_publication(tmp_path):
    conn = connect(str(tmp_path / "official-event-date.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn)
    document = replace(
        housing_document(),
        source_key="nz_parliament_hansard",
        published_at=datetime.fromisoformat("2026-09-05T01:00:00+00:00"),
        raw_metadata={"sitting_date": "2026-09-02"},
    )
    cluster = CivicDocumentCluster(document.dedupe_key, (document,))
    assert event_date_for_cluster(cluster, watch) == date(2026, 9, 2)


def test_date_only_publication_is_not_shifted_by_the_watch_timezone(tmp_path):
    conn = connect(str(tmp_path / "date-only.db"))
    init_schema(conn)
    watch = replace(create_partymap_watch(conn), schedule_timezone="UTC")
    published = datetime.fromisoformat("2026-09-03T00:00:00+12:00")
    document = replace(
        housing_document(),
        published_at=published,
        raw_metadata={
            "publication": PublicationWitness(
                published, "fixture-byline", "date", "03 September 2026",
            ).as_metadata(),
        },
    )
    assert event_date_for_cluster(
        CivicDocumentCluster(document.dedupe_key, (document,)), watch,
    ) == date(2026, 9, 3)


def test_claim_construction_uses_a_dated_basis_despite_undated_corroboration(tmp_path):
    conn = connect(str(tmp_path / "claim-date.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn)
    run = enqueue_civic_run(conn, watch.id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 60, 600)
    get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, T0)
    candidate = ingest_civic_documents(
        conn, run_id=run.id, claim_token=lease.run.claim_token,
        documents=[housing_document()], now=T0,
    ).created_candidates[0]
    for key, tier, day in (
        ("undated-official", CivicEvidenceTier.OFFICIAL, None),
        ("dated-party", CivicEvidenceTier.PARTY_PRIMARY, date(2026, 9, 3)),
    ):
        add_candidate_evidence(
            conn, candidate.id, key, key, f"https://example.test/{key}",
            "Fixture evidence", tier, CivicEvidenceStance.CORROBORATING, T0,
            subject_id="green", claim_category="party-timeline-event", event_date=day,
        )
    assert construct_candidate_claim(conn, candidate).event_date == date(2026, 9, 3)
