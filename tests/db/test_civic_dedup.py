# tests/db/test_civic_dedup.py
"""Turning what this database already published into the pure engine's dedup context.

The windows themselves are `beehive.civic.dedup`'s and are tested there; what has to be proved
here is that the *read* puts the right rows in front of them:

  * sealed membership comes from `sealed_into_batch_at`, not `status`, so a Candidate superseded
    by tomorrow's correction still suppresses a repeat of what it published today;
  * a fingerprint is only "published" once PartyMap has receipted the Batch that carried it, so a
    Batch still in the spool cannot suppress anything;
  * both boundaries are inclusive to the exact second, because "seven days" that silently means
    six days and 23 hours would publish a duplicate one day in seven.
"""
from datetime import datetime, timedelta, timezone

import pytest

from beehive.civic.dedup import evaluate_dedup
from beehive.civic.reasons import CivicPolicyReason
from beehive.db.civic_batches import (bind_batch_payload_hash, claim_batch_for_delivery,
                                      get_or_create_batch_if_claimed, seal_batch_if_claimed)
from beehive.db.civic_candidates import (add_candidate_evidence, create_candidate,
                                         mark_candidate_verified, set_candidate_status,
                                         supersede_candidate)
from beehive.db.civic_consumed_items import claim_consumed_item
from beehive.db.civic_dedup import DEDUP_HORIZON, build_dedup_context
from beehive.db.civic_receipts import record_delivery_receipt
from beehive.db.civic_runs import claim_civic_run, complete_civic_run, enqueue_civic_run
from beehive.db.civic_verifications import record_verification
from beehive.db.civic_watches import create_civic_watch
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import (CivicCandidateStatus, CivicDeliveryChannel,
                                  CivicEvidenceStance, CivicEvidenceTier, CivicRunStatus,
                                  CivicVerificationVerdict)

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)
SEVEN_DAYS = timedelta(days=7)
FOURTEEN_DAYS = timedelta(days=14)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def watch_id(conn):
    return create_civic_watch(conn, "NZ election", "Q", T0).id


def _publish(conn, watch_id, dedupe_key, when, *, deliver=False, url=None, consumed=None):
    """One Candidate taken all the way to sealed, and optionally to receipted."""
    run = enqueue_civic_run(conn, watch_id, when, when)
    lease = claim_civic_run(conn, run.id, when, 600, 3600)
    token = lease.run.claim_token
    batch = get_or_create_batch_if_claimed(conn, run.id, token, when)
    candidate = create_candidate(conn, batch.id, dedupe_key, "Title", "Statement", when)
    add_candidate_evidence(
        conn, candidate.id, "official_feed", f"e-{dedupe_key}",
        url or f"https://example.test/{dedupe_key}", "Release", CivicEvidenceTier.OFFICIAL,
        CivicEvidenceStance.SUPPORTING, when, publisher="Beehive",
        canonical_url=url or f"https://example.test/{dedupe_key}")
    if consumed:
        conn.execute("BEGIN IMMEDIATE")
        for item_key, item_url in consumed:
            claim_consumed_item(
                conn, watch_id=watch_id, run_id=run.id, candidate_id=candidate.id,
                item_key=item_key, source_key=item_key.split(":")[0],
                external_key=item_key.split(":")[1], canonical_url=item_url, now=when)
        conn.commit()
    set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, when)
    record_verification(conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", when)
    mark_candidate_verified(conn, candidate.id, when)
    sealed = seal_batch_if_claimed(conn, batch.id, run.id, token, when)
    if deliver:
        sealed = bind_batch_payload_hash(conn, sealed.id, "f" * 64, when)
        delivery = claim_batch_for_delivery(conn, sealed.id, when, 60)
        record_delivery_receipt(
            conn, sealed.id, delivery.batch.claim_token, CivicDeliveryChannel.SPOOL, "/spool",
            when)
    complete_civic_run(conn, run.id, token, CivicRunStatus.COMPLETED, when)
    return candidate, sealed


def _decide(conn, watch_id, now, *, fingerprint="dk-new", urls=()):
    context = build_dedup_context(conn, watch_id, now)
    return evaluate_dedup(fingerprint=fingerprint, canonical_urls=list(urls), context=context)


# ============================================================================
# Sealed candidates, seven days
# ============================================================================


def test_a_fingerprint_sealed_today_is_a_duplicate(conn, watch_id):
    _publish(conn, watch_id, "dk-1", T0)
    decision = _decide(conn, watch_id, T0 + timedelta(hours=2), fingerprint="dk-1")
    assert decision.is_duplicate
    assert decision.reason is (
        CivicPolicyReason.DUPLICATE_SEALED_CANDIDATE_WITHIN_SEVEN_DAYS)


def test_exactly_seven_days_is_still_inside_the_window(conn, watch_id):
    _publish(conn, watch_id, "dk-1", T0)
    decision = _decide(conn, watch_id, T0 + SEVEN_DAYS, fingerprint="dk-1")
    assert decision.is_duplicate
    assert decision.matched_age == SEVEN_DAYS


def test_one_second_past_seven_days_is_outside_the_window(conn, watch_id):
    _publish(conn, watch_id, "dk-1", T0)
    decision = _decide(
        conn, watch_id, T0 + SEVEN_DAYS + timedelta(seconds=1), fingerprint="dk-1")
    assert not decision.is_duplicate
    assert decision.reason is CivicPolicyReason.DEDUP_OUTSIDE_SEALED_WINDOW


def test_a_superseded_candidate_still_counts_as_sealed(conn, watch_id):
    """Membership is `sealed_into_batch_at`, so tomorrow's correction never un-publishes what
    went out today -- and dedup has to keep saying so."""
    published, _batch = _publish(conn, watch_id, "dk-1", T0)
    later = T0 + timedelta(days=1)
    correction, _ = _publish(conn, watch_id, "dk-2", later)
    supersede_candidate(conn, published.id, correction.id, later)

    decision = _decide(conn, watch_id, later + timedelta(hours=1), fingerprint="dk-1")
    assert decision.is_duplicate


def test_a_still_building_candidate_is_not_yet_sealed(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, T0)
    create_candidate(conn, batch.id, "dk-1", "Title", "Statement", T0)
    decision = _decide(conn, watch_id, T0 + timedelta(hours=1), fingerprint="dk-1")
    assert not decision.is_duplicate
    assert decision.reason is CivicPolicyReason.DEDUP_NO_PRIOR_MATCH


# ============================================================================
# Canonical URLs
# ============================================================================


def test_a_repeat_under_a_new_fingerprint_is_caught_by_its_url(conn, watch_id):
    _publish(conn, watch_id, "dk-1", T0, url="https://example.test/story")
    decision = _decide(
        conn, watch_id, T0 + timedelta(days=1), fingerprint="dk-brand-new",
        urls=["https://example.test/story"])
    assert decision.is_duplicate
    assert decision.reason is CivicPolicyReason.DUPLICATE_CANONICAL_URL


def test_a_consumed_item_url_counts_even_when_it_never_became_a_citation(conn, watch_id):
    """An article can be the reason a Candidate exists without surviving the evidence rules. It
    is still the same article when it comes round again tomorrow."""
    _publish(
        conn, watch_id, "dk-1", T0, url="https://example.test/cited",
        consumed=[("nz_stuff:1", "https://example.test/read-but-not-cited")])
    decision = _decide(
        conn, watch_id, T0 + timedelta(days=1), fingerprint="dk-brand-new",
        urls=["https://example.test/read-but-not-cited"])
    assert decision.is_duplicate
    assert decision.reason is CivicPolicyReason.DUPLICATE_CANONICAL_URL


def test_a_candidate_is_never_compared_against_itself(conn, watch_id):
    candidate, _batch = _publish(conn, watch_id, "dk-1", T0)
    context = build_dedup_context(
        conn, watch_id, T0 + timedelta(hours=1), exclude_candidate_id=candidate.id)
    decision = evaluate_dedup(
        fingerprint="dk-1", canonical_urls=["https://example.test/dk-1"], context=context)
    assert not decision.is_duplicate


# ============================================================================
# Published fingerprints, fourteen days
# ============================================================================


def test_an_undelivered_batch_publishes_no_fingerprint(conn, watch_id):
    """A Batch sitting in the spool has not been published, so it cannot suppress tomorrow's
    Candidate for something that never went out."""
    _publish(conn, watch_id, "dk-1", T0, deliver=False)
    context = build_dedup_context(conn, watch_id, T0 + timedelta(days=10))
    assert context.published_fingerprints == ()


def test_a_delivered_fingerprint_is_a_duplicate_up_to_fourteen_days(conn, watch_id):
    _publish(conn, watch_id, "dk-1", T0, deliver=True)
    # Beyond seven days the sealed window has expired, so only the published window can catch it.
    decision = _decide(conn, watch_id, T0 + FOURTEEN_DAYS, fingerprint="dk-1")
    assert decision.is_duplicate
    assert decision.reason is (
        CivicPolicyReason.DUPLICATE_PUBLISHED_FINGERPRINT_WITHIN_FOURTEEN_DAYS)
    assert decision.matched_age == FOURTEEN_DAYS


def test_one_second_past_fourteen_days_is_outside_the_published_window(conn, watch_id):
    _publish(conn, watch_id, "dk-1", T0, deliver=True)
    decision = _decide(
        conn, watch_id, T0 + FOURTEEN_DAYS + timedelta(seconds=1), fingerprint="dk-1")
    assert not decision.is_duplicate
    assert decision.reason is CivicPolicyReason.DEDUP_OUTSIDE_SEALED_WINDOW


def test_between_seven_and_fourteen_days_the_published_window_is_what_catches_it(
        conn, watch_id):
    _publish(conn, watch_id, "dk-1", T0, deliver=True)
    inside = _decide(conn, watch_id, T0 + timedelta(days=10), fingerprint="dk-1")
    assert inside.is_duplicate
    assert inside.reason is (
        CivicPolicyReason.DUPLICATE_PUBLISHED_FINGERPRINT_WITHIN_FOURTEEN_DAYS)


# ============================================================================
# Bounds and scoping
# ============================================================================


def test_the_read_is_scoped_to_one_watch(conn):
    first = create_civic_watch(conn, "NZ election", "Q", T0).id
    second = create_civic_watch(conn, "Local body", "Q", T0).id
    _publish(conn, first, "dk-1", T0)
    context = build_dedup_context(conn, second, T0 + timedelta(hours=1))
    assert context.sealed_candidates == ()


def test_material_older_than_the_horizon_is_not_loaded(conn, watch_id):
    _publish(conn, watch_id, "dk-1", T0)
    now = T0 + DEDUP_HORIZON + timedelta(days=1)
    context = build_dedup_context(conn, watch_id, now)
    assert context.sealed_candidates == ()
    assert context.published_fingerprints == ()


def test_the_horizon_is_wide_enough_to_still_report_a_near_miss(conn, watch_id):
    """`evaluate_dedup` reports "there is a prior match, but it is outside the window" as a
    finding. Cutting the read at exactly fourteen days would turn that into "no prior match"."""
    _publish(conn, watch_id, "dk-1", T0, deliver=True)
    decision = _decide(conn, watch_id, T0 + timedelta(days=20), fingerprint="dk-1")
    assert not decision.is_duplicate
    assert decision.reason is CivicPolicyReason.DEDUP_OUTSIDE_SEALED_WINDOW


def test_a_future_stamped_row_is_never_offered_to_the_engine(conn, watch_id):
    """CivicDedupContext refuses a record stamped after `now`. Loading one would turn a clock
    skew into an exception in the middle of a decision instead of a row that simply is not
    relevant yet."""
    _publish(conn, watch_id, "dk-1", T0 + timedelta(days=1))
    context = build_dedup_context(conn, watch_id, T0)
    assert context.sealed_candidates == ()


def test_an_empty_watch_produces_an_empty_context(conn, watch_id):
    context = build_dedup_context(conn, watch_id, T0)
    assert context.sealed_candidates == ()
    assert context.published_fingerprints == ()
    assert context.now == T0
