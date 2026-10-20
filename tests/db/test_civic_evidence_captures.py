"""Immutable source capture projection and durable, fenced hydration work."""
from datetime import date, datetime, timedelta, timezone
from dataclasses import replace
from html import escape
import sqlite3

import pytest

from beehive.db.civic_batches import get_or_create_batch_if_claimed
from beehive.db.civic_batches import refresh_candidate_for_ai_if_claimed
from beehive.db.civic_candidates import (
    add_candidate_evidence, create_candidate, get_candidate, list_candidate_evidence,
    mark_candidate_verified, set_candidate_status,
)
from beehive.db.civic_runs import claim_civic_run, enqueue_civic_run
from beehive.db.civic_watches import create_civic_watch
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import (
    CivicCandidateStatus, CivicEvidenceStance, CivicEvidenceTier, CivicVerificationVerdict,
)
from beehive.db.civic_verifications import list_verifications, record_verification
from beehive.civic.evidence_refresh import plan_evidence_refresh, prepare_evidence_refresh
from beehive.civic.production import PartyMediaDocumentHydrator
from beehive.civic.production import build_source_profiles
from tests.civic.civic_stubs import transport_for
from beehive.db.civic_batches import adopt_capacity_deferrals, count_ai_deferrals
from beehive.db.civic_runs import complete_civic_run
from beehive.domain.civic import CivicRunStatus

NOW = datetime(2026, 9, 8, 0, tzinfo=timezone.utc)
BAD_DATE = datetime(2026, 9, 4, 8, 41, tzinfo=timezone.utc)
URL = "https://www.national.org.nz/news/fixture-announcement"
ARTICLE_TITLE = "National announces a community services proposal"
ARTICLE_BODY = (
    "National announced a community services proposal focused on local service access. "
    "The party said it would invite councils and community organisations to identify gaps "
    "before setting delivery priorities. Consultation findings would be published with the "
    "programme's next update. The announcement describes a proposal, not the opening of "
    "new services or an approval of funding."
)


def article_page(*, dated=True):
    publication = (
        '<meta property="article:published_time" content="2026-09-02T09:00:00+12:00">'
        if dated else "")
    return (
        f"<html><head>{publication}</head><body><article>"
        f"<h1>{escape(ARTICLE_TITLE)}</h1><p>{escape(ARTICLE_BODY)}</p>"
        "</article></body></html>"
    )


@pytest.fixture
def context(tmp_path):
    conn = connect(str(tmp_path / "captures.db"))
    init_schema(conn)
    watch = create_civic_watch(conn, "partymap", "What changed?", NOW)
    run = enqueue_civic_run(conn, watch.id, NOW, NOW)
    lease = claim_civic_run(conn, run.id, NOW, 300, 1200)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, NOW)
    candidate = create_candidate(
        conn, batch.id, f"national:{URL}", "Legacy title", "Legacy statement.", NOW)
    yield conn, lease.run, batch, candidate, tmp_path
    conn.close()


def add_evidence(conn, candidate, *, metadata=None, url=URL):
    kwargs = {} if metadata is None else {"source_metadata": metadata}
    return add_candidate_evidence(
        conn, candidate.id, "national", f"national:{url}", url, "Legacy title",
        CivicEvidenceTier.PARTY_PRIMARY, CivicEvidenceStance.SUPPORTING, NOW,
        publisher="New Zealand National Party", publisher_id=build_source_profiles()["national"].publisher_id,
        source_kind="party-primary", party_slug="national", subject_id="national",
        claim_category="party-timeline-event", event_date=date(2026, 9, 4),
        canonical_url=url, excerpt="A complete stored excerpt without reliable date provenance.",
        published_at=BAD_DATE, accessible_full_text=True, **kwargs)


def test_initial_source_metadata_is_persisted_without_losing_official_event_fields(context):
    conn, run, batch, candidate, root = context
    metadata = {
        "event_date": "2026-09-02",
        "event_date_provenance": {
            "value": "2026-09-02T12:00:00+12:00", "source": "official-sitting",
        },
        "source_capture": {"version": 1, "publication": None},
        "updated_at": "2026-09-04T08:41:00+00:00",
    }
    evidence = add_evidence(conn, candidate, metadata=metadata)

    from beehive.db.civic_evidence_captures import list_evidence_capture_inputs

    stored = list_evidence_capture_inputs(conn, candidate.id)
    assert len(stored) == 1
    assert stored[0].evidence.id == evidence.id
    assert stored[0].source_metadata == metadata


async def test_refresh_appends_corrected_capture_without_rewriting_base_or_judgment(context):
    from beehive.db.civic_evidence_captures import (
        apply_evidence_refresh_if_claimed, list_evidence_capture_history,
        list_evidence_capture_inputs, reserve_evidence_refresh_if_claimed,
    )

    conn, run, batch, candidate, root = context
    original = add_evidence(conn, candidate)
    set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, NOW)
    judgment = record_verification(
        conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "claude-opus-5", NOW)
    candidate = mark_candidate_verified(conn, candidate.id, NOW)
    plan = plan_evidence_refresh(
        candidate, batch, list_evidence_capture_inputs(conn, candidate.id), remaining_hydrations=25)
    reservation = reserve_evidence_refresh_if_claimed(
        conn, plan=plan, claim_token=run.claim_token, now=NOW)
    hydrator = PartyMediaDocumentHydrator(transport_for({URL: (article_page(), "text/html")}))

    async def hydrate(document, now):
        return hydrator(document, now)

    prepared = await prepare_evidence_refresh(
        plan, hydrate=hydrate, now_fn=lambda: NOW, deadline_at=NOW + timedelta(minutes=5))
    applied = apply_evidence_refresh_if_claimed(
        conn, reservation=reservation, prepared=prepared, now=NOW)

    assert applied.status == "applied"
    current = list_candidate_evidence(conn, candidate.id)[0]
    assert current.id == original.id
    assert current.title == ARTICLE_TITLE
    assert current.event_date == date(2026, 9, 2)
    assert current.published_at != BAD_DATE
    assert conn.execute(
        "SELECT published_at FROM civic_candidate_evidence WHERE id = ?", (original.id,)
    ).fetchone()[0] == BAD_DATE.isoformat()
    assert list_verifications(conn, candidate.id) == [judgment]
    assert get_candidate(conn, candidate.id).ai_revision == 1
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.DRAFTED
    assert len(list_evidence_capture_history(conn, original.id)) == 1
    metadata = list_evidence_capture_inputs(conn, candidate.id)[0].source_metadata
    assert metadata == prepared.captures[0].document.raw_metadata
    assert metadata["source_capture"]["extraction"]["quality"] == "complete"
    assert metadata["source_capture"]["extraction"]["full_text_chars"] >= 200


async def prepared_refresh(
    context, *, dated=True, drafted=False, researching=False, research_reserved=False,
    extra_url=None, initial_revision=0, undated_urls=(), refresh_before_reservation=False,
):
    from beehive.db.civic_evidence_captures import (
        list_evidence_capture_inputs, reserve_evidence_refresh_if_claimed,
    )

    conn, run, batch, candidate, root = context
    evidence = add_evidence(conn, candidate)
    if extra_url is not None:
        add_evidence(conn, candidate, url=extra_url)
    if researching:
        candidate = set_candidate_status(conn, candidate.id, CivicCandidateStatus.RESEARCHING, NOW)
        if research_reserved:
            from beehive.db.civic_verifications import reserve_research_attempt

            reserve_research_attempt(conn, candidate.id)
    elif not drafted:
        set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, NOW)
        for revision in range(initial_revision):
            refresh_candidate_for_ai_if_claimed(
                conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.claim_token,
                expected_revision=revision, reason="previous_deployment_refresh", now=NOW)
            set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, NOW)
        record_verification(conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "claude-opus-5", NOW)
        candidate = mark_candidate_verified(conn, candidate.id, NOW)
    plan = plan_evidence_refresh(
        candidate, batch, list_evidence_capture_inputs(conn, candidate.id), remaining_hydrations=25)
    if refresh_before_reservation:
        refresh_candidate_for_ai_if_claimed(
            conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.claim_token,
            expected_revision=plan.expected_ai_revision,
            reason="publication_stage_version_changed", now=NOW)
    reservation = reserve_evidence_refresh_if_claimed(
        conn, plan=plan, claim_token=run.claim_token, now=NOW)
    hydrator = PartyMediaDocumentHydrator(transport_for({
        target.document.canonical_url: (
            article_page(dated=dated and target.document.canonical_url not in undated_urls),
            "text/html",
        ) for target in plan.targets}))

    async def hydrate(document, now):
        return hydrator(document, now)

    prepared = await prepare_evidence_refresh(
        plan, hydrate=hydrate, now_fn=lambda: NOW, deadline_at=NOW + timedelta(minutes=5))
    return evidence, plan, reservation, prepared


async def test_explicit_unknown_publication_never_revives_old_lastmod(context):
    from beehive.db.civic_evidence_captures import apply_evidence_refresh_if_claimed, list_evidence_capture_inputs

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(context, dated=False)
    result = apply_evidence_refresh_if_claimed(
        conn, reservation=reservation, prepared=prepared, now=NOW)
    current = list_candidate_evidence(conn, candidate.id)[0]
    assert result.status == "deferred"
    assert result.reason == "source_refresh_date_unavailable"
    assert current.published_at is None
    assert current.event_date is None
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.DEFERRED_AI
    assert get_candidate(conn, candidate.id).rejection_code is None
    assert list_evidence_capture_inputs(conn, candidate.id)[0].source_metadata[
        "source_capture"]["publication"] is None
    assert conn.execute(
        "SELECT published_at FROM civic_candidate_evidence WHERE id = ?", (original.id,)
    ).fetchone()[0] == BAD_DATE.isoformat()


@pytest.mark.parametrize("undated_url", [URL, URL + "-second"])
async def test_mixed_date_refresh_keeps_the_dated_candidate_publishable(context, undated_url):
    from beehive.civic.destination import (
        DESTINATION_V1_ENGINE_VERSION, constrain_qualification_to_destination_v1,
    )
    from beehive.civic.pipeline import qualify_civic_candidate
    from beehive.civic.production import build_ownership_registry, construct_candidate_claim
    from beehive.db.civic_evidence_captures import (
        apply_evidence_refresh_if_claimed, list_evidence_capture_inputs,
        require_current_source_capture,
    )

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(
        context, extra_url=URL + "-second", undated_urls=(undated_url,))
    assert len(prepared.captures) == 2
    result = apply_evidence_refresh_if_claimed(
        conn, reservation=reservation, prepared=prepared, now=NOW)
    assert result.status == "applied"
    assert result.ai_revision == 1
    captures = list_evidence_capture_inputs(conn, candidate.id)
    undated = next(capture for capture in captures if capture.evidence.url == undated_url)
    dated = next(capture for capture in captures if capture.evidence.url != undated_url)
    assert undated.evidence.published_at is None
    assert undated.evidence.event_date is None
    assert undated.source_metadata["source_capture"]["publication"] is None
    assert dated.evidence.event_date == date(2026, 9, 2)
    assert all(row[0] == BAD_DATE.isoformat() for row in conn.execute(
        "SELECT published_at FROM civic_candidate_evidence WHERE candidate_id = ?", (candidate.id,)))
    require_current_source_capture(conn, candidate.id)
    claim = construct_candidate_claim(conn, get_candidate(conn, candidate.id))
    assert claim.event_date == date(2026, 9, 2)
    qualify_civic_candidate(
        conn, run_id=run.id, claim_token=run.claim_token, claim=claim,
        registry=build_ownership_registry(), now=NOW,
        engine_version=DESTINATION_V1_ENGINE_VERSION,
        qualification_scope=constrain_qualification_to_destination_v1)
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.VERIFYING
    assert get_candidate(conn, candidate.id).rejection_code is None


async def test_copied_candidate_date_cannot_certify_an_undated_source_capture(context):
    from beehive.civic.evidence_refresh import EvidenceRefreshStatus
    from beehive.db.civic_evidence_captures import candidate_source_plan

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(context, dated=False)
    document = prepared.captures[0].document
    fresh = create_candidate(
        conn, batch.id, "complete-but-undated-capture", document.title, "An undated statement.", NOW)
    add_candidate_evidence(
        conn, fresh.id, document.source_key, document.dedupe_key, document.canonical_url,
        document.title, document.tier, CivicEvidenceStance.SUPPORTING, NOW,
        publisher=document.publisher, publisher_id=original.publisher_id,
        source_kind="party-primary", party_slug="national", subject_id="national",
        claim_category="party-timeline-event", event_date=date(2026, 9, 4),
        canonical_url=document.canonical_url, excerpt=document.excerpt, published_at=None,
        accessible_full_text=True, source_metadata=document.raw_metadata)
    current = candidate_source_plan(conn, fresh.id)
    assert current.status is EvidenceRefreshStatus.DEFERRED
    assert current.reason == "source_refresh_date_unavailable"
    assert get_candidate(conn, fresh.id).rejection_code is None


@pytest.mark.parametrize("initial_revision", [0, 1])
async def test_source_capture_and_prompt_refresh_cost_one_revision(context, initial_revision):
    from beehive.db.civic_evidence_captures import apply_evidence_refresh_if_claimed

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(
        context, initial_revision=initial_revision)
    pending = refresh_candidate_for_ai_if_claimed(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.claim_token,
        expected_revision=initial_revision, reason="publication_stage_version_changed", now=NOW)
    assert pending.ai_revision == initial_revision + 1
    result = apply_evidence_refresh_if_claimed(
        conn, reservation=reservation, prepared=prepared, now=NOW)
    assert result.status == "applied"
    assert result.ai_revision == initial_revision + 1
    assert get_candidate(conn, candidate.id).ai_revision == initial_revision + 1


@pytest.mark.parametrize("ai_status", [None, "reserved", "failed", "succeeded"])
async def test_source_plan_keeps_original_revision_when_policy_refresh_precedes_reservation(
    context, ai_status,
):
    from beehive.civic.ai_gateway import CIVIC_MODEL_PROVIDER, CivicAiStage
    from beehive.civic.generation import CIVIC_PROMPT_VERSION, CIVIC_SCHEMA_VERSION
    from beehive.db.civic_ai_stages import (
        count_run_ai_calls, list_candidate_stages, record_ai_stage_failure,
        record_ai_stage_success, reserve_ai_stage,
    )
    from beehive.db.civic_evidence_captures import (
        apply_evidence_refresh_if_claimed, list_evidence_capture_history,
    )

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(
        context, initial_revision=1, refresh_before_reservation=True)
    assert plan.expected_ai_revision == 1
    assert get_candidate(conn, candidate.id).ai_revision == 2
    if ai_status is not None:
        set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, NOW)
        attempt = reserve_ai_stage(
            conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.claim_token,
            stage=CivicAiStage.GENERATE, provider=CIVIC_MODEL_PROVIDER, model="gpt-6-astra",
            prompt_version=CIVIC_PROMPT_VERSION, prompt_hash="a" * 64,
            schema_version=CIVIC_SCHEMA_VERSION, input_hash="b" * 64, now=NOW)
        assert attempt is not None
        if ai_status == "failed":
            record_ai_stage_failure(
                conn, reservation=attempt, error_code="fixture_transport_failure", now=NOW)
        elif ai_status == "succeeded":
            record_ai_stage_success(
                conn, reservation=attempt, now=NOW,
                output={"decision": "discard", "editorial_exclusion": "opinion",
                        "reason": "Fixture editorial decision", "entry": None})
    prior_attempts = list_candidate_stages(conn, candidate.id)
    applied = apply_evidence_refresh_if_claimed(
        conn, reservation=reservation, prepared=prepared, now=NOW)
    assert applied.ai_revision == 2
    assert get_candidate(conn, candidate.id).ai_revision == 2
    assert list_candidate_stages(conn, candidate.id) == prior_attempts
    if ai_status is None:
        assert applied.status == "applied"
        assert len(list_evidence_capture_history(conn, original.id)) == 1
    else:
        assert applied.status == "stale"
        assert applied.reason == "source_refresh_conflict"
        assert list_evidence_capture_history(conn, original.id) == []
    assert count_run_ai_calls(conn, run.id) == int(ai_status is not None)
    assert reservation.expected_ai_revision == plan.expected_ai_revision


async def test_recorded_qualification_closes_source_coalescing_before_any_ai_reservation(context):
    from beehive.civic.destination import (
        DESTINATION_V1_ENGINE_VERSION, constrain_qualification_to_destination_v1,
    )
    from beehive.civic.evidence import qualify_candidate
    from beehive.civic.pipeline import (
        evidence_item_for, qualification_input_hash, qualification_output_hash,
    )
    from beehive.civic.production import build_ownership_registry, construct_candidate_claim
    from beehive.db.civic_ai_stages import count_run_ai_calls
    from beehive.db.civic_evidence_captures import (
        apply_evidence_refresh_if_claimed, list_evidence_capture_history,
    )
    from beehive.db.civic_qualifications import get_latest_qualification, record_qualification

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(
        context, initial_revision=1, refresh_before_reservation=True)
    pending = get_candidate(conn, candidate.id)
    assert pending.ai_revision == 2 and pending.status is CivicCandidateStatus.DRAFTED
    claim = construct_candidate_claim(conn, pending)
    registry = build_ownership_registry()
    items = [evidence_item_for(row) for row in list_candidate_evidence(conn, candidate.id)]
    qualification = constrain_qualification_to_destination_v1(
        claim, qualify_candidate(claim, items, registry=registry))
    assert qualification.is_publishable
    conn.execute("BEGIN IMMEDIATE")
    recorded = record_qualification(
        conn, candidate_id=candidate.id, run_id=run.id, qualification=qualification,
        engine_version=DESTINATION_V1_ENGINE_VERSION, research_attempts_used=0, now=NOW,
        input_hash=qualification_input_hash(
            claim, items, registry=registry, dedup_fingerprints=(), dedup_urls=(),
            engine_version=DESTINATION_V1_ENGINE_VERSION),
        output_hash=qualification_output_hash(qualification), record_kind=claim.record_kind)
    conn.commit()

    applied = apply_evidence_refresh_if_claimed(
        conn, reservation=reservation, prepared=prepared, now=NOW)
    assert applied.status == "stale" and applied.reason == "source_refresh_conflict"
    assert get_candidate(conn, candidate.id) == pending
    assert get_latest_qualification(conn, candidate.id) == recorded
    assert list_evidence_capture_history(conn, original.id) == []
    assert count_run_ai_calls(conn, run.id) == 0


async def test_capture_apply_replay_is_explicit_and_rejects_changed_payload(context):
    from beehive.db.civic_evidence_captures import (
        CivicCaptureConflictError, apply_evidence_refresh_if_claimed, list_evidence_capture_history,
    )

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(context)
    first = apply_evidence_refresh_if_claimed(conn, reservation=reservation, prepared=prepared, now=NOW)
    replay = apply_evidence_refresh_if_claimed(conn, reservation=reservation, prepared=prepared, now=NOW)
    assert replay.replayed
    assert replay.capture_ids == first.capture_ids
    assert len(list_evidence_capture_history(conn, original.id)) == 1
    changed = replace(prepared, captures=(replace(
        prepared.captures[0], document=replace(prepared.captures[0].document, title="Changed")),))
    with pytest.raises(CivicCaptureConflictError):
        apply_evidence_refresh_if_claimed(conn, reservation=reservation, prepared=changed, now=NOW)


@pytest.mark.parametrize("failure", ["claim", "lease", "deadline", "membership"])
async def test_stale_capture_callbacks_cannot_apply(context, failure):
    from beehive.db.civic_evidence_captures import apply_evidence_refresh_if_claimed, list_evidence_capture_history

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(context)
    if failure == "claim":
        conn.execute("UPDATE civic_runs SET claim_token = 'new-owner' WHERE id = ?", (run.id,))
    elif failure == "lease":
        conn.execute("UPDATE civic_runs SET lease_expires_at = ? WHERE id = ?", (NOW.isoformat(), run.id))
    elif failure == "deadline":
        conn.execute("UPDATE civic_runs SET deadline_at = ? WHERE id = ?", (NOW.isoformat(), run.id))
    else:
        conn.execute(
            "UPDATE civic_candidates SET status = 'rejected', rejection_code = 'fresh_decision' "
            "WHERE id = ?", (candidate.id,))
    conn.commit()
    assert apply_evidence_refresh_if_claimed(
        conn, reservation=reservation, prepared=prepared, now=NOW) is None
    assert list_evidence_capture_history(conn, original.id) == []
    assert get_candidate(conn, candidate.id).ai_revision == 0


async def test_deadline_after_append_rolls_back_capture_and_ai_revision(context):
    from beehive.db.civic_evidence_captures import apply_evidence_refresh_if_claimed, list_evidence_capture_history

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(context)
    times = iter((NOW, NOW + timedelta(hours=1)))
    assert apply_evidence_refresh_if_claimed(
        conn, reservation=reservation, prepared=prepared, now=NOW,
        now_fn=lambda: next(times)) is None
    assert list_evidence_capture_history(conn, original.id) == []
    assert get_candidate(conn, candidate.id).ai_revision == 0
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.VERIFIED


async def test_sql_failure_rolls_back_capture_and_revision(context):
    from beehive.db.civic_evidence_captures import apply_evidence_refresh_if_claimed, list_evidence_capture_history

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(context)
    conn.execute(
        "CREATE TEMP TRIGGER fail_capture AFTER INSERT ON civic_evidence_captures "
        "BEGIN SELECT RAISE(ABORT, 'fixture capture failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="fixture capture failed"):
        apply_evidence_refresh_if_claimed(conn, reservation=reservation, prepared=prepared, now=NOW)
    assert list_evidence_capture_history(conn, original.id) == []
    assert get_candidate(conn, candidate.id).ai_revision == 0


async def test_later_capture_failure_rolls_back_the_whole_capture_set(context):
    from beehive.db.civic_evidence_captures import (
        apply_evidence_refresh_if_claimed, count_run_hydrations, list_evidence_capture_history,
    )

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(
        context, extra_url=URL + "-second")
    assert reservation.units == 2
    second = plan.targets[1].evidence_id
    conn.execute(
        "CREATE TEMP TRIGGER fail_second_capture AFTER INSERT ON civic_evidence_captures "
        f"WHEN NEW.evidence_id = {second} "
        "BEGIN SELECT RAISE(ABORT, 'second fixture capture failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="second fixture capture failed"):
        apply_evidence_refresh_if_claimed(
            conn, reservation=reservation, prepared=prepared, now=NOW)
    assert all(
        list_evidence_capture_history(conn, target.evidence_id) == [] for target in plan.targets)
    assert get_candidate(conn, candidate.id).ai_revision == 0
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.VERIFIED
    assert count_run_hydrations(conn, run.id) == 2


async def test_changed_active_fingerprint_cannot_overwrite_current_evidence(context):
    from beehive.db.civic_evidence_captures import apply_evidence_refresh_if_claimed, list_evidence_capture_history

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(context)
    conn.execute(
        "UPDATE civic_candidate_evidence SET excerpt = 'concurrently changed material' WHERE id = ?",
        (original.id,))
    conn.commit()
    result = apply_evidence_refresh_if_claimed(conn, reservation=reservation, prepared=prepared, now=NOW)
    assert result.status == "stale"
    assert result.reason == "source_refresh_conflict"
    assert list_evidence_capture_history(conn, original.id) == []
    assert get_candidate(conn, candidate.id).ai_revision == 0


async def test_singleton_fingerprints_cannot_be_swapped_between_reserved_sources(context):
    from beehive.db.civic_evidence_captures import (
        CivicCaptureConflictError, apply_evidence_refresh_if_claimed, count_run_hydrations,
        reserve_evidence_refresh_if_claimed,
    )

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(
        context, extra_url=URL + "-second")
    first, second = plan.targets
    mismatched_plan = replace(plan, targets=(
        replace(first, expected_evidence_fingerprint=second.expected_evidence_fingerprint),
        replace(second, expected_evidence_fingerprint=first.expected_evidence_fingerprint),
    ))
    with pytest.raises(CivicCaptureConflictError, match="changed required captures"):
        reserve_evidence_refresh_if_claimed(
            conn, plan=mismatched_plan, claim_token=run.claim_token, now=NOW)
    with pytest.raises(ValueError, match="does not match its planned evidence"):
        replace(prepared, captures=(
            replace(prepared.captures[0],
                    expected_evidence_fingerprint=second.expected_evidence_fingerprint),
            prepared.captures[1],
        ))
    assert count_run_hydrations(conn, run.id) == 2
    applied = apply_evidence_refresh_if_claimed(
        conn, reservation=reservation, prepared=prepared, now=NOW)
    assert applied.status == "applied" and len(applied.capture_ids) == 2
    assert applied.ai_revision == 1


async def test_fresh_and_refresh_reservations_share_exactly_one_durable_budget(context):
    from beehive.db.civic_evidence_captures import (
        CivicHydrationBudgetError, count_run_hydrations, reserve_hydration_if_claimed,
    )

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(context)
    reserve_hydration_if_claimed(
        conn, run_id=run.id, claim_token=run.claim_token, work_key="fresh-24",
        units=24, now=NOW)
    assert count_run_hydrations(conn, run.id) == 25
    with pytest.raises(CivicHydrationBudgetError):
        reserve_hydration_if_claimed(
            conn, run_id=run.id, claim_token=run.claim_token, work_key="overflow",
            units=1, now=NOW)


async def test_two_connections_cannot_reserve_or_apply_same_refresh_twice(context):
    from beehive.db.civic_evidence_captures import (
        apply_evidence_refresh_if_claimed, count_run_hydrations,
        list_evidence_capture_history, reserve_evidence_refresh_if_claimed,
    )

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(context)
    other = connect(str(root / "captures.db"))
    try:
        assert reserve_evidence_refresh_if_claimed(
            other, plan=plan, claim_token=run.claim_token, now=NOW) is None
        first = apply_evidence_refresh_if_claimed(
            conn, reservation=reservation, prepared=prepared, now=NOW)
        second = apply_evidence_refresh_if_claimed(
            other, reservation=reservation, prepared=prepared, now=NOW)
        assert second.replayed
        assert second.capture_ids == first.capture_ids
        assert len(list_evidence_capture_history(other, original.id)) == 1
        assert count_run_hydrations(other, run.id) == 1
    finally:
        other.close()


async def test_fresh_capture_result_replays_after_restart_without_refetch(context):
    from beehive.db.civic_evidence_captures import (
        CivicCaptureConflictError, CivicCaptureIntegrityError, complete_hydration_if_claimed,
        count_run_hydrations, hydration_documents, reserve_hydration_if_claimed,
    )

    conn, run, batch, candidate, root = context
    original, plan, refresh_reservation, prepared = await prepared_refresh(context)
    requested = plan.targets[0].document
    captured = prepared.captures[0].document
    reservation = reserve_hydration_if_claimed(
        conn, run_id=run.id, claim_token=run.claim_token, work_key="fresh-one",
        units=1, documents=(requested,), now=NOW)
    completed = complete_hydration_if_claimed(
        conn, reservation=reservation, documents=(captured,), now=NOW)
    assert hydration_documents(completed) == (captured,)
    with pytest.raises(CivicCaptureIntegrityError, match="stored hydration result hash changed"):
        hydration_documents(replace(completed, result_hash="f" * 64))
    other = connect(str(root / "captures.db"))
    try:
        replay = reserve_hydration_if_claimed(
            other, run_id=run.id, claim_token=run.claim_token, work_key="fresh-one",
            units=1, documents=(requested,), now=NOW)
        assert replay.status == "completed"
        assert hydration_documents(replay) == (captured,)
        assert count_run_hydrations(other, run.id) == 2
        with pytest.raises(CivicCaptureConflictError):
            complete_hydration_if_claimed(
                other, reservation=reservation,
                documents=(replace(captured, canonical_url=URL + "-wrong"),), now=NOW)
    finally:
        other.close()


def test_abandoned_hydration_stays_charged_and_old_callback_cannot_complete(context):
    from beehive.db.civic_evidence_captures import (
        count_run_hydrations, fail_hydration_if_claimed, list_hydration_attempts,
        reserve_hydration_if_claimed,
    )

    conn, run, batch, candidate, root = context
    first = reserve_hydration_if_claimed(
        conn, run_id=run.id, claim_token=run.claim_token, work_key="abandoned",
        units=2, now=NOW)
    conn.execute("UPDATE civic_runs SET claim_token = 'recovered' WHERE id = ?", (run.id,))
    conn.commit()
    second = reserve_hydration_if_claimed(
        conn, run_id=run.id, claim_token="recovered", work_key="abandoned", units=2, now=NOW)
    assert second.id != first.id
    assert count_run_hydrations(conn, run.id) == 4
    assert not fail_hydration_if_claimed(conn, reservation=first, reason="late_callback", now=NOW)
    assert list_hydration_attempts(conn, run.id)[0].error_code == "reservation_abandoned"
    with pytest.raises(sqlite3.IntegrityError, match="cannot be refunded"):
        conn.execute("DELETE FROM civic_hydration_attempts WHERE id = ?", (first.id,))
    conn.rollback()


def test_source_hold_preserves_news_without_spending_ai_revision_or_ai_lifetime(context):
    from beehive.db.civic_evidence_captures import (
        candidate_source_plan, defer_evidence_refresh_if_claimed, list_evidence_capture_inputs,
    )

    conn, run, batch, candidate, root = context
    original = add_evidence(conn, candidate)
    plan = candidate_source_plan(conn, candidate.id)
    held = defer_evidence_refresh_if_claimed(
        conn, plan=plan, claim_token=run.claim_token,
        reason="source_refresh_unsupported:nz_parliament", now=NOW)
    assert held.status == "deferred"
    assert get_candidate(conn, candidate.id).ai_revision == 0
    assert get_candidate(conn, candidate.id).rejection_code is None
    assert count_ai_deferrals(conn, candidate.id) == 0
    complete_civic_run(conn, run.id, run.claim_token, CivicRunStatus.COMPLETED, NOW)
    later = NOW + timedelta(days=1)
    enqueued = enqueue_civic_run(conn, run.watch_id, later, later)
    lease = claim_civic_run(conn, enqueued.id, later, 300, 1200)
    next_batch = get_or_create_batch_if_claimed(conn, enqueued.id, lease.run.claim_token, later)
    adopt_capacity_deferrals(conn, next_batch.id, enqueued.id, lease.run.claim_token, later)
    resumed = get_candidate(conn, candidate.id)
    assert resumed.status is CivicCandidateStatus.DRAFTED
    assert resumed.ai_revision == 0
    assert list_evidence_capture_inputs(conn, candidate.id)[0].evidence == original


async def test_capture_rows_are_immutable(context):
    from beehive.db.civic_evidence_captures import apply_evidence_refresh_if_claimed

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(context)
    applied = apply_evidence_refresh_if_claimed(
        conn, reservation=reservation, prepared=prepared, now=NOW)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE civic_evidence_captures SET published_at = ? WHERE id = ?",
            (BAD_DATE.isoformat(), applied.capture_ids[0]))
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted independently"):
        conn.execute("DELETE FROM civic_evidence_captures WHERE id = ?", (applied.capture_ids[0],))
    conn.rollback()


@pytest.mark.parametrize("state", ["sealed", "delivered", "quarantined"])
async def test_sealed_or_settled_candidate_refuses_source_apply(context, state):
    from beehive.db.civic_evidence_captures import (
        apply_evidence_refresh_if_claimed, list_evidence_capture_history,
    )

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(context)
    conn.execute(
        "UPDATE civic_candidates SET sealed_into_batch_at = ? WHERE id = ?",
        (NOW.isoformat(), candidate.id))
    conn.execute(
        "UPDATE civic_batches SET status = ?, content_hash = ?, sealed_at = ?, settled_at = ? "
        "WHERE id = ?",
        (state, "a" * 64, NOW.isoformat(), None if state == "sealed" else NOW.isoformat(), batch.id))
    conn.commit()

    assert apply_evidence_refresh_if_claimed(
        conn, reservation=reservation, prepared=prepared, now=NOW) is None
    assert list_evidence_capture_history(conn, original.id) == []
    assert get_candidate(conn, candidate.id).ai_revision == 0


def test_source_failure_retries_are_bounded_without_rejecting_the_news(context):
    from beehive.db.civic_evidence_captures import (
        CivicSourceRefreshBlocked, candidate_source_plan, defer_evidence_refresh_if_claimed,
        reserve_evidence_refresh_if_claimed,
    )

    conn, run, batch, candidate, root = context
    add_evidence(conn, candidate)
    current_run, current_batch, now = run, batch, NOW
    for attempt in range(3):
        plan = candidate_source_plan(conn, candidate.id)
        reservation = reserve_evidence_refresh_if_claimed(
            conn, plan=plan, claim_token=current_run.claim_token, now=now)
        result = defer_evidence_refresh_if_claimed(
            conn, plan=plan, reservation=reservation, claim_token=current_run.claim_token,
            reason="detail_fetch_failed", now=now)
        assert result.status == "deferred"
        assert get_candidate(conn, candidate.id).rejection_code is None
        complete_civic_run(
            conn, current_run.id, current_run.claim_token, CivicRunStatus.COMPLETED, now)
        now += timedelta(days=1)
        enqueued = enqueue_civic_run(conn, run.watch_id, now, now)
        current_run = claim_civic_run(conn, enqueued.id, now, 300, 1200).run
        current_batch = get_or_create_batch_if_claimed(
            conn, current_run.id, current_run.claim_token, now)
        adopt_capacity_deferrals(
            conn, current_batch.id, current_run.id, current_run.claim_token, now)
    plan = candidate_source_plan(conn, candidate.id)
    with pytest.raises(CivicSourceRefreshBlocked, match="source_refresh_retry_exhausted"):
        reserve_evidence_refresh_if_claimed(
            conn, plan=plan, claim_token=current_run.claim_token, now=now)
    held = defer_evidence_refresh_if_claimed(
        conn, plan=plan, claim_token=current_run.claim_token,
        reason="source_refresh_retry_exhausted", now=now)
    assert held.status == "deferred"
    assert get_candidate(conn, candidate.id).ai_revision == 0
    assert get_candidate(conn, candidate.id).rejection_code is None
    assert count_ai_deferrals(conn, candidate.id) == 0


async def test_evidence_source_guard_covers_unsealed_verified_and_partial_ai_candidates(context):
    from beehive.db.civic_evidence_captures import (
        CivicSourceRefreshBlocked, apply_evidence_refresh_if_claimed, require_current_source_capture,
    )

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(context)
    with pytest.raises(CivicSourceRefreshBlocked):
        require_current_source_capture(conn, candidate.id)
    conn.execute(
        "UPDATE civic_candidates SET status = 'verifying', settled_at = NULL WHERE id = ?",
        (candidate.id,))
    conn.commit()
    with pytest.raises(CivicSourceRefreshBlocked):
        require_current_source_capture(conn, candidate.id)
    applied = apply_evidence_refresh_if_claimed(
        conn, reservation=reservation, prepared=prepared, now=NOW)
    assert applied.status == "applied"
    require_current_source_capture(conn, candidate.id)


async def test_initial_official_event_metadata_keeps_unknown_publication(context):
    from beehive.db.civic_evidence_captures import list_evidence_capture_inputs

    conn, run, batch, candidate, root = context
    metadata = {
        "event_date": "2026-09-02", "publication": None,
        "event_date_provenance": {
            "value": "2026-09-02T12:00:00+12:00", "source": "official-sitting",
        },
    }
    evidence = add_candidate_evidence(
        conn, candidate.id, "nz_parliament", "nz_parliament:sitting", "https://parliament.test/sitting",
        "Official sitting", CivicEvidenceTier.OFFICIAL, CivicEvidenceStance.SUPPORTING, NOW,
        publisher="Parliament", publisher_id="parliament", source_kind="parliament",
        subject_id="national", claim_category="legislation",
        canonical_url="https://parliament.test/sitting", event_date=date(2026, 9, 2),
        published_at=None, excerpt="The official sitting record records proceedings.",
        accessible_full_text=True, source_metadata=metadata)
    current = list_evidence_capture_inputs(conn, candidate.id)[0]
    assert current.source_metadata == metadata
    assert current.evidence.id == evidence.id
    assert current.evidence.published_at is None
    assert current.evidence.event_date == date(2026, 9, 2)


async def test_capture_storage_preserves_old_ai_receipts_and_qualification(context):
    from beehive.db.civic_ai_stages import (
        get_stage_receipt, list_candidate_stages, record_ai_stage_success, reserve_ai_stage,
    )
    from beehive.civic.ai_gateway import CivicAiStage
    from beehive.db.civic_evidence_captures import apply_evidence_refresh_if_claimed
    from beehive.civic.destination import DESTINATION_V1_ENGINE_VERSION
    from beehive.civic.evidence import qualify_candidate
    from beehive.civic.pipeline import evidence_item_for, qualification_output_hash
    from beehive.civic.production import build_ownership_registry, construct_candidate_claim
    from beehive.db.civic_qualifications import get_latest_qualification, record_qualification

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(context)
    old = reserve_ai_stage(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.claim_token,
        stage=CivicAiStage.GENERATE, provider="github-copilot", model="claude-haiku-4.5",
        prompt_version="civic-ai-prompts/3", prompt_hash="a" * 64,
        schema_version="civic-ai-schema/1", input_hash="b" * 64, now=NOW)
    receipt = record_ai_stage_success(
        conn, reservation=old, output={"legacy": "immutable normalized output"}, now=NOW)
    claim = construct_candidate_claim(conn, get_candidate(conn, candidate.id))
    qualification = qualify_candidate(
        claim, [evidence_item_for(original)], registry=build_ownership_registry())
    assert qualification.is_publishable
    conn.execute("BEGIN IMMEDIATE")
    record_qualification(
        conn, candidate_id=candidate.id, run_id=run.id, qualification=qualification,
        engine_version=DESTINATION_V1_ENGINE_VERSION, input_hash="c" * 64,
        output_hash=qualification_output_hash(qualification), research_attempts_used=0,
        now=NOW, record_kind=claim.record_kind)
    conn.commit()
    before = conn.execute(
        "SELECT * FROM civic_qualifications WHERE candidate_id = ?", (candidate.id,)).fetchall()
    applied = apply_evidence_refresh_if_claimed(
        conn, reservation=reservation, prepared=prepared, now=NOW)
    assert applied.ai_revision == 1
    assert receipt in list_candidate_stages(conn, candidate.id)
    assert get_stage_receipt(conn, candidate.id, CivicAiStage.GENERATE) is None
    assert get_latest_qualification(conn, candidate.id) is None
    assert conn.execute(
        "SELECT * FROM civic_qualifications WHERE candidate_id = ?", (candidate.id,)).fetchall() == before


async def test_whole_watch_deletion_cascades_without_breaking_capture_history(context):
    from beehive.db.civic_evidence_captures import apply_evidence_refresh_if_claimed
    from beehive.db.civic_watches import hard_delete_civic_watch

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(context)
    apply_evidence_refresh_if_claimed(conn, reservation=reservation, prepared=prepared, now=NOW)
    assert hard_delete_civic_watch(conn, run.watch_id)
    assert conn.execute("SELECT COUNT(*) FROM civic_evidence_captures").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM civic_hydration_attempts").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("pending", [False, True])
async def test_researching_candidates_can_apply_without_losing_spent_research(context, pending):
    from beehive.db.civic_evidence_captures import apply_evidence_refresh_if_claimed

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(
        context, researching=True, research_reserved=pending)
    applied = apply_evidence_refresh_if_claimed(
        conn, reservation=reservation, prepared=prepared, now=NOW)
    assert applied.status == "applied"
    current = get_candidate(conn, candidate.id)
    assert current.ai_revision == 1
    assert current.research_attempt_count == int(pending)
    assert current.status is (
        CivicCandidateStatus.RESEARCHING if pending else CivicCandidateStatus.DRAFTED)
    assert list_candidate_evidence(conn, candidate.id)[0].event_date == date(2026, 9, 2)


async def test_apply_after_candidate_is_deferred_is_a_stale_callback_not_sql_error(context):
    from beehive.db.civic_evidence_captures import (
        apply_evidence_refresh_if_claimed, list_evidence_capture_history,
    )
    from beehive.db.civic_batches import defer_candidate_for_ai

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(context)
    pending = refresh_candidate_for_ai_if_claimed(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.claim_token,
        expected_revision=0, reason="publication_stage_version_changed", now=NOW)
    defer_candidate_for_ai(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.claim_token,
        reason="ai_budget_exhausted", now=NOW)
    assert apply_evidence_refresh_if_claimed(
        conn, reservation=reservation, prepared=prepared, now=NOW) is None
    assert list_evidence_capture_history(conn, original.id) == []
    assert get_candidate(conn, candidate.id).ai_revision == pending.ai_revision


async def test_stale_source_failure_cannot_replace_an_existing_ai_deferral(context):
    from beehive.db.civic_batches import defer_candidate_for_ai
    from beehive.db.civic_evidence_captures import defer_evidence_refresh_if_claimed

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(context)
    refresh_candidate_for_ai_if_claimed(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.claim_token,
        expected_revision=0, reason="publication_stage_version_changed", now=NOW)
    defer_candidate_for_ai(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.claim_token,
        reason="ai_budget_exhausted", now=NOW)
    before = get_candidate(conn, candidate.id)
    assert defer_evidence_refresh_if_claimed(
        conn, plan=plan, reservation=reservation, claim_token=run.claim_token,
        reason="late_detail_fetch_failure", now=NOW) is None
    assert get_candidate(conn, candidate.id) == before
    assert count_ai_deferrals(conn, candidate.id) == 1
    complete_civic_run(conn, run.id, run.claim_token, CivicRunStatus.COMPLETED, NOW)
    later = NOW + timedelta(days=1)
    enqueued = enqueue_civic_run(conn, run.watch_id, later, later)
    next_run = claim_civic_run(conn, enqueued.id, later, 300, 1200).run
    next_batch = get_or_create_batch_if_claimed(
        conn, next_run.id, next_run.claim_token, later)
    adopt_capacity_deferrals(conn, next_batch.id, next_run.id, next_run.claim_token, later)
    complete_civic_run(
        conn, next_run.id, next_run.claim_token, CivicRunStatus.FAILED, later,
        error_code="worker_interrupted")
    assert count_ai_deferrals(conn, candidate.id) == 2


async def test_completed_source_capture_does_not_disable_ai_failure_lifetime(context):
    from beehive.db.civic_evidence_captures import apply_evidence_refresh_if_claimed

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(context)
    apply_evidence_refresh_if_claimed(conn, reservation=reservation, prepared=prepared, now=NOW)
    complete_civic_run(
        conn, run.id, run.claim_token, CivicRunStatus.FAILED, NOW, error_code="worker_interrupted")
    assert count_ai_deferrals(conn, candidate.id) == 1
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.DEFERRED_AI


async def test_legacy_drafted_capture_refresh_starts_one_bounded_ai_revision(context):
    from beehive.db.civic_evidence_captures import apply_evidence_refresh_if_claimed

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(context, drafted=True)
    applied = apply_evidence_refresh_if_claimed(
        conn, reservation=reservation, prepared=prepared, now=NOW)
    assert applied.ai_revision == 1
    complete_civic_run(
        conn, run.id, run.claim_token, CivicRunStatus.FAILED, NOW, error_code="worker_interrupted")
    assert count_ai_deferrals(conn, candidate.id) == 1
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.DEFERRED_AI


async def test_refreshed_pending_research_survives_adoption_and_requalification(context):
    from beehive.civic.pipeline import qualify_civic_candidate, resume_civic_research
    from beehive.civic.production import build_ownership_registry, construct_candidate_claim
    from beehive.db.civic_evidence_captures import apply_evidence_refresh_if_claimed
    from beehive.db.civic_verifications import record_research_attempt_if_claimed
    from beehive.domain.civic import CivicResearchOutcome

    conn, run, batch, candidate, root = context
    original, plan, reservation, prepared = await prepared_refresh(
        context, researching=True, research_reserved=True)
    apply_evidence_refresh_if_claimed(
        conn, reservation=reservation, prepared=prepared, now=NOW)
    complete_civic_run(
        conn, run.id, run.claim_token, CivicRunStatus.FAILED, NOW, error_code="worker_interrupted")
    later = NOW + timedelta(days=1)
    enqueued = enqueue_civic_run(conn, run.watch_id, later, later)
    next_run = claim_civic_run(conn, enqueued.id, later, 300, 1200).run
    next_batch = get_or_create_batch_if_claimed(
        conn, next_run.id, next_run.claim_token, later)
    adopt_capacity_deferrals(conn, next_batch.id, next_run.id, next_run.claim_token, later)
    current = get_candidate(conn, candidate.id)
    assert current.status is CivicCandidateStatus.RESEARCHING
    assert current.ai_revision == 1
    assert resume_civic_research(conn, candidate.id) == 1
    record_research_attempt_if_claimed(
        conn, candidate_id=candidate.id, run_id=next_run.id, claim_token=next_run.claim_token,
        attempt_number=1, outcome=CivicResearchOutcome.UNRESOLVED, started_at=NOW, now=later)
    result = qualify_civic_candidate(
        conn, run_id=next_run.id, claim_token=next_run.claim_token,
        claim=construct_candidate_claim(conn, get_candidate(conn, candidate.id)),
        registry=build_ownership_registry(), now=later)
    assert result is not None
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.VERIFYING
    assert get_candidate(conn, candidate.id).research_attempt_count == 1


@pytest.mark.parametrize("researching", [False, True])
async def test_interrupted_source_fetch_preserves_unsealed_work_before_any_ai_revision(
    context, researching,
):
    conn, run, batch, candidate, root = context
    await prepared_refresh(
        context, drafted=not researching, researching=researching,
        research_reserved=researching)
    complete_civic_run(
        conn, run.id, run.claim_token, CivicRunStatus.FAILED, NOW,
        error_code="interrupted_during_source_fetch")
    current = get_candidate(conn, candidate.id)
    assert current.status is CivicCandidateStatus.DEFERRED_AI
    assert current.rejection_code is None
    assert current.ai_revision == 0
    assert count_ai_deferrals(conn, candidate.id) == 0


def test_partial_candidate_revision_requires_explicit_source_refresh(context):
    conn, run, batch, candidate, root = context
    with pytest.raises(sqlite3.IntegrityError, match="AI refresh requires"):
        conn.execute(
            "UPDATE civic_candidates SET ai_revision = ai_revision + 1 WHERE id = ?",
            (candidate.id,))
    conn.rollback()


def test_source_revision_limit_is_checked_before_spending_hydration_budget(context):
    from beehive.db.civic_evidence_captures import (
        CivicSourceRefreshBlocked, candidate_source_plan, count_run_hydrations,
        reserve_evidence_refresh_if_claimed,
    )

    conn, run, batch, candidate, root = context
    add_evidence(conn, candidate)
    for revision in range(3):
        set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, NOW)
        refresh_candidate_for_ai_if_claimed(
            conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.claim_token,
            expected_revision=revision, reason="publication_stage_version_changed", now=NOW)
    set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, NOW)
    plan = candidate_source_plan(conn, candidate.id)
    with pytest.raises(CivicSourceRefreshBlocked, match="source_refresh_ai_revision_limit"):
        reserve_evidence_refresh_if_claimed(
            conn, plan=plan, claim_token=run.claim_token, now=NOW)
    assert count_run_hydrations(conn, run.id) == 0
    assert get_candidate(conn, candidate.id).rejection_code is None


def test_new_official_capture_is_current_with_source_event_and_unknown_publication(context):
    from beehive.civic.catalog import PARLIAMENT_BILLS
    from beehive.civic.connectors.parliament import ParliamentApiConnector
    from beehive.db.civic_evidence_captures import (
        list_evidence_capture_inputs, require_current_source_capture,
    )
    from tests.civic.civic_stubs import fixture_bytes

    conn, run, batch, candidate, root = context
    transport = transport_for({
        PARLIAMENT_BILLS.endpoint.url: (
            fixture_bytes("parliament_bills_search.json"), "application/json"),
    })
    document = ParliamentApiConnector(PARLIAMENT_BILLS, transport).fetch(NOW).documents[0]
    profile = build_source_profiles()[document.source_key]
    assert document.published_at is None
    evidence = add_candidate_evidence(
        conn, candidate.id, document.source_key, document.dedupe_key, document.canonical_url,
        document.title, document.tier, CivicEvidenceStance.SUPPORTING, NOW,
        publisher=document.publisher, publisher_id=profile.publisher_id,
        source_kind=profile.source_kind.value, subject_id="national", claim_category="legislation",
        canonical_url=document.canonical_url, excerpt=document.excerpt,
        published_at=document.published_at, event_date=date.fromisoformat(
            document.raw_metadata["event_date"]),
        accessible_full_text=True, source_metadata=document.raw_metadata)
    require_current_source_capture(conn, candidate.id)
    current = list_evidence_capture_inputs(conn, candidate.id)[0]
    assert current.evidence == evidence
    assert current.source_metadata == document.raw_metadata
    assert "source_capture" not in current.source_metadata


def test_initial_capture_persistence_refuses_an_expired_lease(context):
    from beehive.db.civic_candidates import add_candidate_evidence_if_claimed

    conn, run, batch, candidate, root = context
    conn.execute(
        "UPDATE civic_runs SET lease_expires_at = ? WHERE id = ?", (NOW.isoformat(), run.id))
    conn.commit()
    result = add_candidate_evidence_if_claimed(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.claim_token,
        source_ref="national", external_key=f"national:{URL}", url=URL, title="Captured title",
        tier=CivicEvidenceTier.PARTY_PRIMARY, stance=CivicEvidenceStance.SUPPORTING,
        now=NOW, source_metadata={"capture": "must not be written by an expired worker"})
    assert result is None
    assert list_candidate_evidence(conn, candidate.id) == []


def test_hydration_budget_is_shared_and_survives_reconnection(context):
    from beehive.db.civic_evidence_captures import (
        CivicHydrationBudgetError, count_run_hydrations, reserve_hydration_if_claimed,
    )

    conn, run, batch, candidate, root = context
    first = reserve_hydration_if_claimed(
        conn, run_id=run.id, claim_token=run.claim_token, work_key="fresh-documents",
        units=20, now=NOW)
    assert first.units == 20
    other = connect(str(root / "captures.db"))
    try:
        assert count_run_hydrations(other, run.id) == 20
        reserve_hydration_if_claimed(
            other, run_id=run.id, claim_token=run.claim_token, work_key="carryover-documents",
            units=5, now=NOW)
        with pytest.raises(CivicHydrationBudgetError):
            reserve_hydration_if_claimed(
                conn, run_id=run.id, claim_token=run.claim_token, work_key="one-too-many",
                units=1, now=NOW)
        assert count_run_hydrations(conn, run.id) == 25
    finally:
        other.close()


def test_a_source_hold_ends_after_its_bounded_lifetime(context):
    """A capture that never succeeds must not circle between daily Runs forever."""
    from beehive.db.civic_evidence_captures import (
        SOURCE_HOLD_LIFETIME, CivicSourceRefreshBlocked, candidate_source_plan,
        defer_evidence_refresh_if_claimed, reserve_evidence_refresh_if_claimed,
    )

    conn, run, batch, candidate, root = context
    add_evidence(conn, candidate)
    current_run, now, results = run, NOW, []
    while now - NOW <= SOURCE_HOLD_LIFETIME:
        plan = candidate_source_plan(conn, candidate.id)
        try:
            reservation = reserve_evidence_refresh_if_claimed(
                conn, plan=plan, claim_token=current_run.claim_token, now=now)
            reason = "detail_fetch_failed"
        except CivicSourceRefreshBlocked as exc:
            reservation, reason = None, exc.reason
        results.append((now, defer_evidence_refresh_if_claimed(
            conn, plan=plan, reservation=reservation, claim_token=current_run.claim_token,
            reason=reason, now=now)))
        if results[-1][1].status != "deferred":
            break
        complete_civic_run(
            conn, current_run.id, current_run.claim_token, CivicRunStatus.COMPLETED, now)
        now += timedelta(days=1)
        enqueued = enqueue_civic_run(conn, run.watch_id, now, now)
        current_run = claim_civic_run(conn, enqueued.id, now, 300, 1200).run
        current_batch = get_or_create_batch_if_claimed(
            conn, current_run.id, current_run.claim_token, now)
        adopt_capacity_deferrals(
            conn, current_batch.id, current_run.id, current_run.claim_token, now)

    held_until, settled = results[-1]
    assert all(result.status == "deferred" for _, result in results[:-1])
    assert held_until - NOW == SOURCE_HOLD_LIFETIME
    assert settled.status == "rejected"
    assert settled.reason == "source_refresh_retry_exhausted"
    rejected = get_candidate(conn, candidate.id)
    assert rejected.status is CivicCandidateStatus.REJECTED
    assert rejected.rejection_code == "source_hold_expired"
    assert count_ai_deferrals(conn, candidate.id) == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM civic_capacity_deferrals WHERE candidate_id = ? "
        "AND adopted_at IS NULL", (candidate.id,)).fetchone()[0] == 0


def test_an_uncapturable_outlets_feed_date_cannot_clear_the_source_date_gate(context):
    """Stuff articles are never captured, so their feed date must not stand in for a capture."""
    from beehive.civic.source_extraction import publication_witness
    from beehive.db.civic_evidence_captures import candidate_source_plan

    conn, run, batch, candidate, root = context
    url = "https://www.stuff.co.nz/politics/361038702/fixture-report"
    published = datetime(2026, 9, 6, 3, 13, tzinfo=timezone.utc)
    add_candidate_evidence(
        conn, candidate.id, "stuff", f"stuff:{url}", url, "A fixture report",
        CivicEvidenceTier.REPUTABLE_MEDIA, CivicEvidenceStance.CORROBORATING, NOW,
        publisher="Stuff", publisher_id="stuff", source_kind="independent-news",
        subject_id="national", claim_category="party-timeline-event",
        event_date=date(2026, 9, 6), canonical_url=url,
        excerpt="A feed summary of the fixture report.", published_at=published,
        source_metadata={"publication": publication_witness(
            published.isoformat(), source="feed-published").as_metadata()},
    )

    plan = candidate_source_plan(conn, candidate.id)
    assert plan.targets == ()
    assert (plan.status.value, plan.reason) == ("deferred", "source_refresh_date_unavailable")
