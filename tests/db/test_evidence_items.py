from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.connection import connect, init_schema
from beehive.db.evidence_items import (get_evidence_item, get_evidence_items,
                                        list_evidence_items_for_session, upsert_evidence_item,
                                        upsert_evidence_item_if_claimed)
from beehive.db.research_runs import (claim_research_run, enqueue_research_run,
                                       recover_expired_research_runs)
from beehive.db.research_sessions import create_research_session
from beehive.db.research_sources import create_research_source
from beehive.domain.research import EvidenceQuality, ResearchSourceOrigin

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
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0).id
    return session_id, source_id


def test_upsert_evidence_item_creates_row_with_citation_number_one(conn, session_and_source):
    session_id, source_id = session_and_source
    item = upsert_evidence_item(
        conn, session_id, source_id, "ext-1", "Title", "https://x/1",
        EvidenceQuality.REPORTING, T0)
    assert item.citation_number == 1
    assert item.session_id == session_id
    assert item.research_source_id == source_id


def test_upsert_evidence_item_allocates_sequential_citation_numbers(conn, session_and_source):
    session_id, source_id = session_and_source
    first = upsert_evidence_item(
        conn, session_id, source_id, "ext-1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0)
    second = upsert_evidence_item(
        conn, session_id, source_id, "ext-2", "T2", "https://x/2",
        EvidenceQuality.PRIMARY, T0)
    assert first.citation_number == 1
    assert second.citation_number == 2


def test_upsert_evidence_item_reuses_row_and_keeps_citation_number_stable(
        conn, session_and_source):
    session_id, source_id = session_and_source
    first = upsert_evidence_item(
        conn, session_id, source_id, "ext-1", "Original title", "https://x/1",
        EvidenceQuality.REPORTING, T0)
    refreshed = upsert_evidence_item(
        conn, session_id, source_id, "ext-1", "Updated title", "https://x/1-new",
        EvidenceQuality.PRIMARY, T0, snippet="new snippet")

    assert refreshed.id == first.id
    assert refreshed.citation_number == first.citation_number
    assert refreshed.title == "Updated title"
    assert refreshed.url == "https://x/1-new"
    assert refreshed.quality == EvidenceQuality.PRIMARY
    assert refreshed.snippet == "new snippet"


def test_upsert_evidence_item_different_sources_can_share_external_key(conn, session_and_source):
    session_id, source_id = session_and_source
    other_source_id = create_research_source(
        conn, session_id, "rss", {}, ResearchSourceOrigin.PLAN, T0).id

    first = upsert_evidence_item(
        conn, session_id, source_id, "same-key", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0)
    second = upsert_evidence_item(
        conn, session_id, other_source_id, "same-key", "T2", "https://x/2",
        EvidenceQuality.REPORTING, T0)
    assert first.id != second.id
    assert second.citation_number == 2  # still a genuinely new canonical item


def test_get_evidence_item_returns_none_for_missing_id(conn):
    assert get_evidence_item(conn, 999) is None


def test_get_evidence_items_batches_lookup(conn, session_and_source):
    session_id, source_id = session_and_source
    first = upsert_evidence_item(
        conn, session_id, source_id, "ext-1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0)
    second = upsert_evidence_item(
        conn, session_id, source_id, "ext-2", "T2", "https://x/2",
        EvidenceQuality.REPORTING, T0)
    result = get_evidence_items(conn, [first.id, second.id, 999])
    assert set(result.keys()) == {first.id, second.id}


def test_get_evidence_items_empty_list_returns_empty_dict(conn):
    assert get_evidence_items(conn, []) == {}


def test_list_evidence_items_for_session_ordered_by_citation_number(conn, session_and_source):
    session_id, source_id = session_and_source
    upsert_evidence_item(
        conn, session_id, source_id, "ext-2", "T2", "https://x/2",
        EvidenceQuality.REPORTING, T0)
    upsert_evidence_item(
        conn, session_id, source_id, "ext-1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0)
    items = list_evidence_items_for_session(conn, session_id)
    assert [i.citation_number for i in items] == [1, 2]


# -- upsert_evidence_item_if_claimed: claim-fenced writes -------------------------------

@pytest.fixture
def claimed_run(conn, session_and_source):
    session_id, source_id = session_and_source
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)
    return session_id, source_id, run.id, claimed.run.claim_token


def test_upsert_evidence_item_if_claimed_inserts_with_valid_claim(conn, claimed_run):
    session_id, source_id, run_id, claim_token = claimed_run
    item = upsert_evidence_item_if_claimed(
        conn, run_id, claim_token, session_id, source_id, "ext-1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0)
    assert item is not None
    assert item.citation_number == 1
    assert item.session_id == session_id


def test_upsert_evidence_item_if_claimed_updates_existing_and_keeps_citation_number(
        conn, claimed_run):
    session_id, source_id, run_id, claim_token = claimed_run
    first = upsert_evidence_item_if_claimed(
        conn, run_id, claim_token, session_id, source_id, "ext-1", "Original", "https://x/1",
        EvidenceQuality.REPORTING, T0)
    second = upsert_evidence_item_if_claimed(
        conn, run_id, claim_token, session_id, source_id, "ext-1", "Updated", "https://x/1-new",
        EvidenceQuality.PRIMARY, T0, snippet="new snippet")
    assert second.id == first.id
    assert second.citation_number == first.citation_number
    assert second.title == "Updated"
    assert second.url == "https://x/1-new"
    assert second.quality == EvidenceQuality.PRIMARY
    assert second.snippet == "new snippet"


def test_upsert_evidence_item_if_claimed_preserves_session_wide_citation_allocation(
        conn, claimed_run):
    session_id, source_id, run_id, claim_token = claimed_run
    # a plain, non-claim-fenced upsert for the same session must still see the claim-fenced
    # writer's already-allocated citation numbers and never reuse one
    upsert_evidence_item_if_claimed(
        conn, run_id, claim_token, session_id, source_id, "ext-1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0)
    other = upsert_evidence_item(
        conn, session_id, source_id, "ext-2", "T2", "https://x/2",
        EvidenceQuality.REPORTING, T0)
    assert other.citation_number == 2


def test_upsert_evidence_item_if_claimed_returns_none_for_wrong_claim_token(conn, claimed_run):
    session_id, source_id, run_id, _ = claimed_run
    result = upsert_evidence_item_if_claimed(
        conn, run_id, "not-the-real-token", session_id, source_id, "ext-1", "T1",
        "https://x/1", EvidenceQuality.REPORTING, T0)
    assert result is None
    assert list_evidence_items_for_session(conn, session_id) == []


def test_upsert_evidence_item_if_claimed_returns_none_for_non_processing_run(
        conn, session_and_source):
    session_id, source_id = session_and_source
    run = enqueue_research_run(conn, session_id, T0)  # left 'pending', never claimed
    result = upsert_evidence_item_if_claimed(
        conn, run.id, "irrelevant-token", session_id, source_id, "ext-1", "T1",
        "https://x/1", EvidenceQuality.REPORTING, T0)
    assert result is None
    assert list_evidence_items_for_session(conn, session_id) == []


def test_upsert_evidence_item_if_claimed_returns_none_for_session_mismatch(
        conn, claimed_run):
    session_id, source_id, run_id, claim_token = claimed_run
    other_session_id = create_research_session(conn, "Other question", T0).id
    result = upsert_evidence_item_if_claimed(
        conn, run_id, claim_token, other_session_id, source_id, "ext-1", "T1",
        "https://x/1", EvidenceQuality.REPORTING, T0)
    assert result is None
    assert list_evidence_items_for_session(conn, session_id) == []
    assert list_evidence_items_for_session(conn, other_session_id) == []


def test_upsert_evidence_item_if_claimed_preserve_flag_keeps_full_text_on_none(
        conn, claimed_run):
    session_id, source_id, run_id, claim_token = claimed_run
    first = upsert_evidence_item_if_claimed(
        conn, run_id, claim_token, session_id, source_id, "ext-1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0, full_text="deep fetched body")
    assert first.full_text == "deep fetched body"

    refreshed = upsert_evidence_item_if_claimed(
        conn, run_id, claim_token, session_id, source_id, "ext-1", "T1 refreshed",
        "https://x/1", EvidenceQuality.REPORTING, T0, snippet="new snippet",
        full_text=None, preserve_existing_full_text=True)
    assert refreshed.full_text == "deep fetched body"
    assert refreshed.title == "T1 refreshed"
    assert refreshed.snippet == "new snippet"


def test_upsert_evidence_item_if_claimed_preserve_flag_still_overwrites_when_new_text_given(
        conn, claimed_run):
    session_id, source_id, run_id, claim_token = claimed_run
    upsert_evidence_item_if_claimed(
        conn, run_id, claim_token, session_id, source_id, "ext-1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0, full_text="first body")
    refreshed = upsert_evidence_item_if_claimed(
        conn, run_id, claim_token, session_id, source_id, "ext-1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0, full_text="second body",
        preserve_existing_full_text=True)
    assert refreshed.full_text == "second body"


def test_upsert_evidence_item_if_claimed_without_preserve_flag_clears_full_text_on_none(
        conn, claimed_run):
    session_id, source_id, run_id, claim_token = claimed_run
    upsert_evidence_item_if_claimed(
        conn, run_id, claim_token, session_id, source_id, "ext-1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0, full_text="deep fetched body")
    refreshed = upsert_evidence_item_if_claimed(
        conn, run_id, claim_token, session_id, source_id, "ext-1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0, full_text=None, preserve_existing_full_text=False)
    assert refreshed.full_text is None


def test_upsert_evidence_item_if_claimed_no_side_effects_after_claim_stolen_via_recovery(
        tmp_path, conn, session_and_source):
    """Real two-connection regression for the stale-worker evidence-clobber race: connection A
    claims a run, its lease is allowed to expire and is recovered + reclaimed by connection B,
    and then A's now-stale claim_token must be rejected with zero side effects -- no insert, no
    update, no citation_number consumed -- even though A believes it still owns the run."""
    session_id, source_id = session_and_source
    other_conn = connect(str(tmp_path / "test.db"))
    init_schema(other_conn)

    run = enqueue_research_run(conn, session_id, T0)
    claimed_a = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)
    stale_claim_token = claimed_a.run.claim_token

    # A's lease expires; a reconciliation sweep (as if run by a different worker/process)
    # requeues the run, and connection B reclaims it with a brand-new claim_token.
    later = T0 + timedelta(minutes=5)
    recover_expired_research_runs(other_conn, later)
    claimed_b = claim_research_run(
        other_conn, run.id, later, lease_seconds=60, deadline_seconds=3600)
    assert claimed_b.run.claim_token != stale_claim_token

    # Connection B (the new, legitimate claimant) writes evidence first.
    legit_item = upsert_evidence_item_if_claimed(
        other_conn, run.id, claimed_b.run.claim_token, session_id, source_id, "ext-1",
        "Legit title", "https://x/1", EvidenceQuality.REPORTING, later)
    assert legit_item is not None
    assert legit_item.citation_number == 1

    # Connection A, still using its now-stale claim_token, must be rejected outright: no
    # clobbering of the row B just wrote, no new row, no citation_number consumed.
    stale_result = upsert_evidence_item_if_claimed(
        conn, run.id, stale_claim_token, session_id, source_id, "ext-1",
        "Stale clobber attempt", "https://evil/1", EvidenceQuality.REPORTING, later)
    assert stale_result is None

    reloaded = get_evidence_item(conn, legit_item.id)
    assert reloaded.title == "Legit title"
    assert reloaded.url == "https://x/1"

    # A brand-new external_key from the stale worker must not sneak in as an insert either.
    stale_new_item_result = upsert_evidence_item_if_claimed(
        conn, run.id, stale_claim_token, session_id, source_id, "ext-2",
        "Stale new item", "https://evil/2", EvidenceQuality.REPORTING, later)
    assert stale_new_item_result is None
    items = list_evidence_items_for_session(conn, session_id)
    assert len(items) == 1
    assert items[0].id == legit_item.id

    other_conn.close()
