# tests/db/test_civic_batch_capacity.py
"""The daily publication cap, and what happens to the Candidates it holds back.

Plan 5.2 caps a slot at six Candidates normally and ten in an election period. The cap is a
*capacity* limit, and the whole point of this suite is that nothing about a capped day may look
like a failure: the overflow keeps its evidence, its verification, its qualification receipt and
the source items it owns, never touches the envelope that was sealed without it, and is adopted
by a later Run without being decided again.

The seal is where the cap is applied, under the same BEGIN IMMEDIATE and the same claim fencing
that already freezes the envelope, so a reader can never observe a Batch whose hash disagrees
with what it published.
"""
from datetime import datetime, timedelta, timezone

import pytest

from beehive.civic.batch_policy import (CivicRankedCandidate, CivicRunMode, published_cap,
                                        select_batch)
from beehive.civic.evidence import CivicRecordKind
from beehive.db.civic_batches import (_policy_id, _queue_for_selection,
                                      adopt_capacity_deferrals, claim_batch_for_delivery,
                                      count_pending_capacity_deferrals, get_batch_for_run,
                                      get_civic_batch, get_or_create_batch_if_claimed,
                                      list_batch_capacity_deferrals,
                                      list_candidate_capacity_deferrals,
                                      list_pending_capacity_deferrals, list_sealed_candidate_ids,
                                      recompute_content_hash, seal_batch_if_claimed)
from beehive.db.civic_candidates import (add_candidate_evidence, create_candidate, get_candidate,
                                         list_batch_candidates, list_candidate_evidence,
                                         mark_candidate_verified, reject_candidate,
                                         set_candidate_status)
from beehive.db.civic_consumed_items import (claim_consumed_item, get_consuming_candidate_id,
                                             list_candidate_items)
from beehive.db.civic_receipts import list_receipts, record_delivery_receipt
from beehive.db.civic_runs import claim_civic_run, complete_civic_run, enqueue_civic_run
from beehive.db.civic_verifications import list_verifications, record_verification
from beehive.db.civic_watches import create_civic_watch
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import (CivicBatchStatus, CivicCandidateStatus, CivicDeliveryChannel,
                                  CivicEvidenceStance, CivicEvidenceTier, CivicRunStatus,
                                  CivicVerificationVerdict)

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def watch_id(conn):
    return create_civic_watch(conn, "NZ election", "Q", T0).id


class Slot:
    """One claimed Run with its BUILDING Batch: everything a seal needs."""

    def __init__(self, conn, watch_id, slot_at, mode="normal"):
        run = enqueue_civic_run(conn, watch_id, slot_at, slot_at, run_mode=mode)
        lease = claim_civic_run(conn, run.id, slot_at, 600, 3600)
        self.id = run.id
        self.token = lease.run.claim_token
        self.at = slot_at
        self.batch = get_or_create_batch_if_claimed(conn, run.id, self.token, slot_at)

    def seal(self, conn, now=None):
        return seal_batch_if_claimed(conn, self.batch.id, self.id, self.token, now or self.at)

    def adopt(self, conn, now=None, **kwargs):
        return adopt_capacity_deferrals(
            conn, self.batch.id, self.id, self.token, now or self.at, **kwargs)

    def complete(self, conn, now=None):
        return complete_civic_run(
            conn, self.id, self.token, CivicRunStatus.COMPLETED, now or self.at)


def _verified(conn, slot, key, *, now=None, consume=None):
    """A Candidate taken to VERIFIED the ordinary way: evidence, a supporting verdict, publish."""
    at = now or slot.at
    candidate = create_candidate(conn, slot.batch.id, key, f"Title {key}", f"Statement {key}", at)
    add_candidate_evidence(
        conn, candidate.id, "official_feed", f"e-{key}", f"https://example.test/{key}",
        "Release", CivicEvidenceTier.OFFICIAL, CivicEvidenceStance.SUPPORTING, at,
        publisher="Beehive")
    if consume:
        conn.execute("BEGIN IMMEDIATE")
        claim_consumed_item(
            conn, watch_id=slot.batch.watch_id, run_id=slot.id, candidate_id=candidate.id,
            item_key=consume, source_key=consume.split(":")[0],
            external_key=consume.split(":")[1], canonical_url=f"https://example.test/{consume}",
            now=at)
        conn.commit()
    set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, at)
    record_verification(conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", at)
    return mark_candidate_verified(conn, candidate.id, at)


def _many(conn, slot, count, prefix="dk"):
    return [_verified(conn, slot, f"{prefix}-{index:02d}") for index in range(count)]


def _status(conn, candidate_id):
    return get_candidate(conn, candidate_id).status


# ============================================================================
# The caps themselves
# ============================================================================


def test_the_plan_caps_are_six_and_ten():
    assert published_cap(CivicRunMode.NORMAL) == 6
    assert published_cap(CivicRunMode.ELECTION) == 10


def test_eight_verified_candidates_seal_six_and_carry_two(conn, watch_id):
    slot = Slot(conn, watch_id, T0)
    candidates = _many(conn, slot, 8)
    sealed = slot.seal(conn)

    assert sealed.candidate_count == 6
    published = [candidate.id for candidate in candidates[:6]]
    carried = [candidate.id for candidate in candidates[6:]]
    assert list_sealed_candidate_ids(conn, sealed.id) == published
    assert all(_status(conn, cid) is CivicCandidateStatus.VERIFIED for cid in published)
    assert all(
        _status(conn, cid) is CivicCandidateStatus.DEFERRED_CAPACITY for cid in carried)


def test_twelve_election_candidates_seal_ten_and_carry_two(conn, watch_id):
    slot = Slot(conn, watch_id, T0, mode="election")
    candidates = _many(conn, slot, 12)
    sealed = slot.seal(conn)

    assert sealed.candidate_count == 10
    assert list_sealed_candidate_ids(conn, sealed.id) == [
        candidate.id for candidate in candidates[:10]]
    assert count_pending_capacity_deferrals(conn, watch_id) == 2


def test_a_run_under_the_cap_carries_nothing(conn, watch_id):
    slot = Slot(conn, watch_id, T0)
    _many(conn, slot, 3)
    sealed = slot.seal(conn)
    assert sealed.candidate_count == 3
    assert count_pending_capacity_deferrals(conn, watch_id) == 0
    assert list_batch_capacity_deferrals(conn, sealed.id) == []


def test_the_election_cap_comes_from_the_run_not_the_caller(conn, watch_id):
    """`run_mode` is fixed when the Run is enqueued. Nothing at seal time -- and nothing a source
    published -- can widen how much a day is allowed to say."""
    slot = Slot(conn, watch_id, T0, mode="election")
    _many(conn, slot, 12)
    assert slot.seal(conn).candidate_count == 10

    with pytest.raises(ValueError, match="unknown Civic Run mode"):
        enqueue_civic_run(conn, watch_id, T0 + timedelta(days=1), T0, run_mode="unlimited")


def test_selection_order_is_exactly_select_batch(conn, watch_id):
    """The seal must publish precisely what the pure policy chose, including its tie-break -- so
    the same queue run through select_batch directly names the same Candidates."""
    slot = Slot(conn, watch_id, T0)
    candidates = _many(conn, slot, 9)
    sealed = slot.seal(conn)

    # The queue the seal built, reconstructed independently and run through the pure policy.
    queue = [
        CivicRankedCandidate(
            candidate_id=_policy_id(candidate.id), rank=rank, fingerprint=candidate.dedupe_key,
            record_kind=CivicRecordKind.TIMELINE)
        for rank, candidate in enumerate(candidates, start=1)]
    selection = select_batch(queue, mode=CivicRunMode.NORMAL)
    assert list_sealed_candidate_ids(conn, sealed.id) == [
        int(policy_id) for policy_id in selection.published_candidate_ids]
    assert [deferral.candidate_id for deferral in list_batch_capacity_deferrals(
        conn, sealed.id)] == [
            int(policy_id) for policy_id in selection.deferred_candidate_ids]
    # Nothing is left to queue once the seal has run: every Candidate was either stamped or
    # carried, so the policy really did decide all of them.
    assert _queue_for_selection(conn, sealed.id) == ([], {})


# ============================================================================
# What the overflow keeps
# ============================================================================


def test_a_carried_candidate_never_enters_the_envelope_it_missed(conn, watch_id):
    slot = Slot(conn, watch_id, T0)
    candidates = _many(conn, slot, 8)
    sealed = slot.seal(conn)
    carried = candidates[7]

    assert get_candidate(conn, carried.id).sealed_into_batch_at is None
    assert carried.id not in list_sealed_candidate_ids(conn, sealed.id)
    assert recompute_content_hash(conn, sealed.id) == sealed.content_hash


def test_the_sealed_hash_does_not_move_when_the_overflow_is_adopted(conn, watch_id):
    slot = Slot(conn, watch_id, T0)
    _many(conn, slot, 8)
    sealed = slot.seal(conn)
    original_hash = sealed.content_hash
    slot.complete(conn)

    later = Slot(conn, watch_id, T0 + timedelta(days=1))
    later.adopt(conn)
    later.seal(conn)

    assert recompute_content_hash(conn, sealed.id) == original_hash
    assert get_civic_batch(conn, sealed.id).candidate_count == 6


def test_a_carried_candidate_keeps_its_evidence_and_verification(conn, watch_id):
    slot = Slot(conn, watch_id, T0)
    candidates = _many(conn, slot, 8)
    slot.seal(conn)
    carried = candidates[7]

    assert len(list_candidate_evidence(conn, carried.id)) == 1
    assert len(list_verifications(conn, carried.id)) == 1
    assert get_candidate(conn, carried.id).settled_at == T0


def test_a_carried_candidate_still_owns_the_items_it_consumed(conn, watch_id):
    slot = Slot(conn, watch_id, T0)
    _many(conn, slot, 7)
    overflow = _verified(conn, slot, "dk-99", consume="nz_stuff:99")
    slot.seal(conn)

    assert _status(conn, overflow.id) is CivicCandidateStatus.DEFERRED_CAPACITY
    assert [item.item_key for item in list_candidate_items(conn, overflow.id)] == [
        "nz_stuff:99"]
    assert get_consuming_candidate_id(conn, watch_id, "nz_stuff:99") == overflow.id


def test_a_carried_candidate_refuses_new_evidence(conn, watch_id):
    """It has already been verified and qualified on what it has; a later Run adopts that
    decision rather than re-taking it, so the basis must not move underneath it."""
    slot = Slot(conn, watch_id, T0)
    candidates = _many(conn, slot, 8)
    slot.seal(conn)
    with pytest.raises(ValueError, match="cannot add Civic Evidence to a deferred_capacity"):
        add_candidate_evidence(
            conn, candidates[7].id, "official_feed", "late", "https://example.test/late", "L",
            CivicEvidenceTier.OFFICIAL, CivicEvidenceStance.SUPPORTING, T0)


def test_the_deferral_records_where_it_was_held_back_from(conn, watch_id):
    slot = Slot(conn, watch_id, T0)
    candidates = _many(conn, slot, 8)
    sealed = slot.seal(conn)

    deferrals = list_batch_capacity_deferrals(conn, sealed.id)
    assert [deferral.candidate_id for deferral in deferrals] == [
        candidates[6].id, candidates[7].id]
    assert all(deferral.from_batch_id == sealed.id for deferral in deferrals)
    assert all(deferral.from_run_id == slot.id for deferral in deferrals)
    assert all(deferral.published_cap == 6 for deferral in deferrals)
    assert all(deferral.run_mode is CivicRunMode.NORMAL for deferral in deferrals)
    assert all(deferral.is_open for deferral in deferrals)


# ============================================================================
# Adoption
# ============================================================================


def test_the_next_run_adopts_the_carryover_with_no_new_documents(conn, watch_id):
    """An empty news day is exactly when there is room for yesterday's overflow, so adoption
    cannot be reachable only through the ingest path."""
    first = Slot(conn, watch_id, T0)
    candidates = _many(conn, first, 8)
    first.seal(conn)
    first.complete(conn)

    second = Slot(conn, watch_id, T0 + timedelta(days=1))
    result = second.adopt(conn)
    assert result.adopted_count == 2
    assert result.adopted_candidate_ids == (candidates[6].id, candidates[7].id)

    sealed = second.seal(conn)
    assert sealed.candidate_count == 2
    assert list_sealed_candidate_ids(conn, sealed.id) == [candidates[6].id, candidates[7].id]
    assert count_pending_capacity_deferrals(conn, watch_id) == 0


def test_adoption_moves_only_run_and_batch_scoped_fields(conn, watch_id):
    first = Slot(conn, watch_id, T0)
    candidates = _many(conn, first, 7)
    first.seal(conn)
    first.complete(conn)
    carried = get_candidate(conn, candidates[6].id)

    second = Slot(conn, watch_id, T0 + timedelta(days=1))
    second.adopt(conn, now=T0 + timedelta(days=1))
    adopted = get_candidate(conn, carried.id)

    assert adopted.run_id == second.id and adopted.batch_id == second.batch.id
    assert adopted.status is CivicCandidateStatus.VERIFIED
    assert adopted.watch_id == carried.watch_id
    assert adopted.dedupe_key == carried.dedupe_key
    assert adopted.title == carried.title
    assert adopted.statement == carried.statement
    assert adopted.created_at == carried.created_at
    assert adopted.settled_at == carried.settled_at
    assert adopted.research_attempt_count == carried.research_attempt_count
    assert adopted.sealed_into_batch_at is None


def test_adoption_never_creates_a_second_candidate(conn, watch_id):
    first = Slot(conn, watch_id, T0)
    _many(conn, first, 7)
    first.seal(conn)
    first.complete(conn)
    before = conn.execute("SELECT COUNT(*) AS n FROM civic_candidates").fetchone()["n"]

    second = Slot(conn, watch_id, T0 + timedelta(days=1))
    second.adopt(conn)
    assert conn.execute("SELECT COUNT(*) AS n FROM civic_candidates").fetchone()["n"] == before
    assert len(list_batch_candidates(conn, second.batch.id)) == 1


def test_adoption_is_idempotent(conn, watch_id):
    first = Slot(conn, watch_id, T0)
    _many(conn, first, 7)
    first.seal(conn)
    first.complete(conn)

    second = Slot(conn, watch_id, T0 + timedelta(days=1))
    assert second.adopt(conn).adopted_count == 1
    assert second.adopt(conn).adopted_count == 0
    assert second.adopt(conn).adopted_count == 0
    assert len(list_batch_candidates(conn, second.batch.id)) == 1


def test_adoption_closes_the_deferral_with_an_auditable_destination(conn, watch_id):
    first = Slot(conn, watch_id, T0)
    candidates = _many(conn, first, 7)
    sealed = first.seal(conn)
    first.complete(conn)
    carried = candidates[6]

    tomorrow = T0 + timedelta(days=1)
    second = Slot(conn, watch_id, tomorrow)
    second.adopt(conn, now=tomorrow)

    history = list_candidate_capacity_deferrals(conn, carried.id)
    assert len(history) == 1
    assert history[0].from_batch_id == sealed.id
    assert history[0].to_batch_id == second.batch.id
    assert history[0].to_run_id == second.id
    assert history[0].adopted_at == tomorrow
    assert not history[0].is_open
    assert list_pending_capacity_deferrals(conn, watch_id) == []


def test_repeated_deferral_keeps_the_whole_carry_history(conn, watch_id):
    """Held back on Saturday, held back again on Sunday, published on Monday: three states of the
    same Candidate that must all stay readable."""
    saturday = Slot(conn, watch_id, T0)
    candidates = _many(conn, saturday, 14)
    first_batch = saturday.seal(conn)
    saturday.complete(conn)
    assert first_batch.candidate_count == 6
    carried = candidates[13]

    sunday_at = T0 + timedelta(days=1)
    sunday = Slot(conn, watch_id, sunday_at)
    assert sunday.adopt(conn, now=sunday_at).adopted_count == 8
    second_batch = sunday.seal(conn, now=sunday_at)
    sunday.complete(conn, now=sunday_at)
    assert second_batch.candidate_count == 6
    assert _status(conn, carried.id) is CivicCandidateStatus.DEFERRED_CAPACITY

    monday_at = T0 + timedelta(days=2)
    monday = Slot(conn, watch_id, monday_at)
    monday.adopt(conn, now=monday_at)
    third_batch = monday.seal(conn, now=monday_at)
    assert carried.id in list_sealed_candidate_ids(conn, third_batch.id)

    history = list_candidate_capacity_deferrals(conn, carried.id)
    assert [(entry.from_batch_id, entry.to_batch_id) for entry in history] == [
        (first_batch.id, sunday.batch.id), (second_batch.id, monday.batch.id)]
    assert [entry.is_open for entry in history] == [False, False]


def test_the_oldest_carryover_is_never_overtaken_by_todays_news(conn, watch_id):
    """Rank is creation order, so a Candidate held back yesterday outranks everything found this
    morning. Without that a busy Watch could carry the same statement forever."""
    saturday = Slot(conn, watch_id, T0)
    yesterday = _many(conn, saturday, 8)
    saturday.seal(conn)
    saturday.complete(conn)

    sunday_at = T0 + timedelta(days=1)
    sunday = Slot(conn, watch_id, sunday_at)
    sunday.adopt(conn, now=sunday_at)
    today = [_verified(conn, sunday, f"sun-{index}", now=sunday_at) for index in range(6)]
    sealed = sunday.seal(conn, now=sunday_at)

    published = list_sealed_candidate_ids(conn, sealed.id)
    assert published[:2] == [yesterday[6].id, yesterday[7].id]
    assert published[2:] == [candidate.id for candidate in today[:4]]


def test_adoption_is_fenced_on_the_claim(conn, watch_id):
    first = Slot(conn, watch_id, T0)
    _many(conn, first, 7)
    first.seal(conn)
    first.complete(conn)

    second = Slot(conn, watch_id, T0 + timedelta(days=1))
    assert adopt_capacity_deferrals(
        conn, second.batch.id, second.id, "not-the-token", T0 + timedelta(days=1)) is None
    assert count_pending_capacity_deferrals(conn, watch_id) == 1


def test_adoption_refuses_a_sealed_batch(conn, watch_id):
    first = Slot(conn, watch_id, T0)
    _many(conn, first, 7)
    first.seal(conn)
    first.complete(conn)

    second = Slot(conn, watch_id, T0 + timedelta(days=1))
    second.seal(conn, now=T0 + timedelta(days=1))
    assert second.adopt(conn, now=T0 + timedelta(days=1)) is None
    assert count_pending_capacity_deferrals(conn, watch_id) == 1


def test_adoption_is_scoped_to_one_watch(conn):
    first_watch = create_civic_watch(conn, "NZ election", "Q", T0).id
    other_watch = create_civic_watch(conn, "Local body", "Q", T0).id
    first = Slot(conn, first_watch, T0)
    _many(conn, first, 7)
    first.seal(conn)
    first.complete(conn)

    other = Slot(conn, other_watch, T0 + timedelta(days=1))
    assert other.adopt(conn, now=T0 + timedelta(days=1)).adopted_count == 0
    assert count_pending_capacity_deferrals(conn, first_watch) == 1


def test_a_carryover_may_be_rejected_instead_of_carried_forever(conn, watch_id):
    first = Slot(conn, watch_id, T0)
    candidates = _many(conn, first, 7)
    first.seal(conn)
    carried = candidates[6]
    reject_candidate(conn, carried.id, "stale", T0 + timedelta(days=3))
    assert _status(conn, carried.id) is CivicCandidateStatus.REJECTED
    assert count_pending_capacity_deferrals(conn, watch_id) == 0


# ============================================================================
# Sealing, settlement and the rest of the envelope contract
# ============================================================================


def test_an_unverified_candidate_still_blocks_the_seal(conn, watch_id):
    slot = Slot(conn, watch_id, T0)
    _many(conn, slot, 7)
    create_candidate(conn, slot.batch.id, "dk-open", "Open", "Statement", T0)
    with pytest.raises(ValueError, match="still unsettled"):
        slot.seal(conn)
    assert count_pending_capacity_deferrals(conn, watch_id) == 0


def test_rejected_candidates_are_neither_published_nor_carried(conn, watch_id):
    slot = Slot(conn, watch_id, T0)
    candidates = _many(conn, slot, 7)
    dropped = create_candidate(conn, slot.batch.id, "dk-bad", "Bad", "Statement", T0)
    reject_candidate(conn, dropped.id, "screened_out", T0)
    sealed = slot.seal(conn)

    assert sealed.candidate_count == 6
    assert _status(conn, dropped.id) is CivicCandidateStatus.REJECTED
    assert [deferral.candidate_id for deferral in list_batch_capacity_deferrals(
        conn, sealed.id)] == [candidates[6].id]


def test_a_capacity_deferred_candidate_does_not_block_run_completion(conn, watch_id):
    """Settled for its slot without being final: the Run finishes cleanly while the Candidate
    stays adoptable."""
    slot = Slot(conn, watch_id, T0)
    _many(conn, slot, 8)
    slot.seal(conn)
    assert slot.complete(conn) is True
    assert count_pending_capacity_deferrals(conn, watch_id) == 2


def test_a_replayed_seal_changes_nothing(conn, watch_id):
    slot = Slot(conn, watch_id, T0)
    _many(conn, slot, 8)
    first = slot.seal(conn)
    second = slot.seal(conn, now=T0 + timedelta(minutes=5))

    assert second.content_hash == first.content_hash
    assert second.candidate_count == first.candidate_count
    assert second.sealed_at == first.sealed_at
    assert len(list_batch_capacity_deferrals(conn, first.id)) == 2
    assert count_pending_capacity_deferrals(conn, watch_id) == 2


def test_a_slot_with_nothing_to_publish_seals_empty(conn, watch_id):
    """No Candidates of its own and no carryovers: an envelope with nothing in it, which is what
    lets a quiet civic day open no pull request at all."""
    slot = Slot(conn, watch_id, T0)
    assert slot.adopt(conn).adopted_count == 0
    sealed = slot.seal(conn)
    assert sealed.candidate_count == 0
    assert list_sealed_candidate_ids(conn, sealed.id) == []
    assert count_pending_capacity_deferrals(conn, watch_id) == 0


def test_one_batch_per_auckland_day_survives_the_carryover(conn, watch_id):
    first = Slot(conn, watch_id, T0)
    _many(conn, first, 8)
    first.seal(conn)
    first.complete(conn)

    second = Slot(conn, watch_id, T0 + timedelta(days=1))
    second.adopt(conn)
    second.seal(conn, now=T0 + timedelta(days=1))

    assert conn.execute(
        "SELECT COUNT(*) AS n FROM civic_batches").fetchone()["n"] == 2
    assert get_batch_for_run(conn, first.id).id == first.batch.id
    assert get_batch_for_run(conn, second.id).id == second.batch.id
    # Re-serving the same slot resumes the same Run and therefore the same envelope.
    assert enqueue_civic_run(conn, watch_id, T0, T0).id == first.id


def test_the_carryover_is_delivered_once_and_only_once(conn, watch_id):
    from beehive.db.civic_batches import bind_batch_payload_hash

    first = Slot(conn, watch_id, T0)
    _many(conn, first, 8)
    first_sealed = first.seal(conn)
    first_sealed = bind_batch_payload_hash(conn, first_sealed.id, "a" * 64, T0)
    first_delivery = claim_batch_for_delivery(conn, first_sealed.id, T0, 60)
    record_delivery_receipt(
        conn, first_sealed.id, first_delivery.batch.claim_token, CivicDeliveryChannel.SPOOL,
        "/spool", T0)
    first.complete(conn)

    tomorrow = T0 + timedelta(days=1)
    second = Slot(conn, watch_id, tomorrow)
    second.adopt(conn, now=tomorrow)
    second_sealed = second.seal(conn, now=tomorrow)
    second_sealed = bind_batch_payload_hash(conn, second_sealed.id, "b" * 64, tomorrow)
    second_delivery = claim_batch_for_delivery(conn, second_sealed.id, tomorrow, 60)
    record_delivery_receipt(
        conn, second_sealed.id, second_delivery.batch.claim_token, CivicDeliveryChannel.SPOOL,
        "/spool", tomorrow)

    receipts = list_receipts(conn)
    assert len(receipts) == 2
    assert len({receipt.idempotency_key for receipt in receipts}) == 2
    assert get_civic_batch(conn, first_sealed.id).status is CivicBatchStatus.DELIVERED
    assert get_civic_batch(conn, second_sealed.id).status is CivicBatchStatus.DELIVERED
