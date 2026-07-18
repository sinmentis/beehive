import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.connection import connect, init_schema
from beehive.db.evidence_items import upsert_evidence_item
from beehive.db.evidence_state import create_evidence_state_revision
from beehive.db.research_runs import (claim_research_run, complete_research_run,
                                       enqueue_research_run, get_research_run,
                                       request_cancel_research_run)
from beehive.db.research_sessions import create_research_session
from beehive.db.research_snapshots import create_snapshot, seal_snapshot
from beehive.db.research_sources import create_research_source
from beehive.db.research_syntheses import (SynthesisAdmissionStatus, SynthesisPersistFailureReason,
                                            admit_synthesis_if_claimed, create_synthesis,
                                            create_synthesis_if_claimed, get_latest_synthesis,
                                            get_synthesis, list_syntheses)
from beehive.domain.research import (ClaimProvenance, EvidenceCitation, EvidenceQuality,
                                      ResearchRunStatus, ResearchSourceOrigin, SufficiencyState,
                                      SynthesisClaim, SynthesisSection)

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


def _extra_connection(tmp_path):
    other = connect(str(tmp_path / "test.db"))
    init_schema(other)
    return other


@pytest.fixture
def scenario(conn):
    session_id = create_research_session(conn, "Q", T0).id
    source_id = create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0).id
    run_id = enqueue_research_run(conn, session_id, T0).id
    snapshot_id = create_snapshot(conn, session_id, run_id, T0).id
    item = upsert_evidence_item(
        conn, session_id, source_id, "e1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0)
    seal_snapshot(conn, snapshot_id, T0)
    revision = create_evidence_state_revision(conn, session_id, snapshot_id, [item.id], T0)
    return session_id, revision.id, item.id, item.citation_number


@pytest.fixture
def claimed_scenario(conn):
    """Like `scenario`, but the Research Run backing the Evidence Snapshot is actually claimed
    (status='processing') so create_synthesis_if_claimed's run-claim fence can be exercised."""
    session_id = create_research_session(conn, "Q", T0).id
    source_id = create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0).id
    run = enqueue_research_run(conn, session_id, T0)
    lease = claim_research_run(conn, run.id, T0, lease_seconds=600, deadline_seconds=1200)
    snapshot_id = create_snapshot(conn, session_id, lease.run.id, T0).id
    item = upsert_evidence_item(
        conn, session_id, source_id, "e1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0)
    seal_snapshot(conn, snapshot_id, T0)
    revision = create_evidence_state_revision(conn, session_id, snapshot_id, [item.id], T0)
    return session_id, lease.run.id, lease.run.claim_token, revision.id, item.id, item.citation_number


def _evidence_claim(item_id, citation_number, text="Rates rose"):
    return SynthesisClaim(
        text=text, section=SynthesisSection.BOTTOM_LINE, provenance=ClaimProvenance.EVIDENCE,
        citations=(EvidenceCitation(evidence_item_id=item_id, citation_number=citation_number),))


def test_create_synthesis_starts_at_version_one(conn, scenario):
    session_id, revision_id, item_id, citation_number = scenario
    claim = _evidence_claim(item_id, citation_number)
    synthesis = create_synthesis(
        conn, session_id, revision_id, SufficiencyState.PARTIAL, (claim,), "gpt-5", "en", T0)
    assert synthesis.version == 1
    assert synthesis.claims == (claim,)
    assert synthesis.sufficiency_state == SufficiencyState.PARTIAL


def test_create_synthesis_persists_model_knowledge_claim_without_citations(conn, scenario):
    session_id, revision_id, _, _ = scenario
    claim = SynthesisClaim(
        text="Background context", section=SynthesisSection.MODEL_KNOWLEDGE,
        provenance=ClaimProvenance.MODEL_KNOWLEDGE)
    synthesis = create_synthesis(
        conn, session_id, revision_id, SufficiencyState.INSUFFICIENT, (claim,), "gpt-5", "en", T0)
    assert synthesis.claims[0].citations == ()


# ============================================================================
# section: persisted as its own claims_json field, validated at write/read time (never at
# rendering, and never encoded as a text prefix)
# ============================================================================

def test_create_synthesis_persists_section_as_its_own_claims_json_field(conn, scenario):
    session_id, revision_id, item_id, citation_number = scenario
    claim = _evidence_claim(item_id, citation_number, text="Key finding")
    synthesis = create_synthesis(
        conn, session_id, revision_id, SufficiencyState.PARTIAL, (claim,), "gpt-5", "en", T0)
    row = conn.execute(
        "SELECT claims_json FROM research_syntheses WHERE id = ?", (synthesis.id,)).fetchone()
    raw_claims = json.loads(row["claims_json"])
    assert raw_claims == [{
        "text": "Key finding", "section": "bottom_line", "provenance": "evidence",
    }]
    assert synthesis.claims[0].section is SynthesisSection.BOTTOM_LINE
    assert synthesis.claims[0].text == "Key finding"


def test_get_synthesis_fails_at_read_time_when_section_is_missing(conn, scenario):
    session_id, revision_id, item_id, citation_number = scenario
    claim = _evidence_claim(item_id, citation_number)
    synthesis = create_synthesis(
        conn, session_id, revision_id, SufficiencyState.PARTIAL, (claim,), "gpt-5", "en", T0)
    malformed = json.dumps([{"text": claim.text, "provenance": "evidence"}])
    conn.execute(
        "UPDATE research_syntheses SET claims_json = ? WHERE id = ?",
        (malformed, synthesis.id))
    with pytest.raises(ValueError, match="missing 'section'"):
        get_synthesis(conn, synthesis.id)


def test_get_synthesis_fails_at_read_time_when_section_is_unknown(conn, scenario):
    session_id, revision_id, item_id, citation_number = scenario
    claim = _evidence_claim(item_id, citation_number)
    synthesis = create_synthesis(
        conn, session_id, revision_id, SufficiencyState.PARTIAL, (claim,), "gpt-5", "en", T0)
    malformed = json.dumps(
        [{"text": claim.text, "section": "not_a_real_section", "provenance": "evidence"}])
    conn.execute(
        "UPDATE research_syntheses SET claims_json = ? WHERE id = ?",
        (malformed, synthesis.id))
    with pytest.raises(ValueError, match="unknown section"):
        get_synthesis(conn, synthesis.id)


def test_create_synthesis_rejects_section_provenance_mismatch_before_any_write(conn, scenario):
    """The domain constructor -- not this repository -- is what rejects a malformed
    section/provenance pairing, and it does so before create_synthesis ever opens a
    transaction: no row is written to either research_syntheses or its claims_json."""
    session_id, revision_id, item_id, citation_number = scenario
    with pytest.raises(ValueError, match="needs EVIDENCE provenance"):
        SynthesisClaim(
            text="Mislabeled", section=SynthesisSection.KEY_FINDINGS,
            provenance=ClaimProvenance.MODEL_KNOWLEDGE)
    assert list_syntheses(conn, session_id) == []


def test_create_synthesis_requires_at_least_one_claim(conn, scenario):
    session_id, revision_id, _, _ = scenario
    with pytest.raises(ValueError, match="at least one claim"):
        create_synthesis(
            conn, session_id, revision_id, SufficiencyState.PARTIAL, (), "gpt-5", "en", T0)


def test_create_synthesis_is_append_only_and_increments_version(conn, scenario):
    session_id, revision_id, item_id, citation_number = scenario
    claim = _evidence_claim(item_id, citation_number)
    first = create_synthesis(
        conn, session_id, revision_id, SufficiencyState.PARTIAL, (claim,), "gpt-5", "en", T0)
    second = create_synthesis(
        conn, session_id, revision_id, SufficiencyState.SUFFICIENT, (claim,), "gpt-5", "en",
        T0 + timedelta(minutes=1))
    assert second.version == first.version + 1
    # the earlier version is still readable unchanged
    assert get_synthesis(conn, first.id).sufficiency_state == SufficiencyState.PARTIAL


def test_multiple_claims_keep_citations_grouped_by_claim_index(conn, scenario):
    session_id, revision_id, item_id, citation_number = scenario
    claim_a = _evidence_claim(item_id, citation_number, text="First claim")
    claim_b = SynthesisClaim(text="Second claim (no citation)",
                              section=SynthesisSection.MODEL_KNOWLEDGE,
                              provenance=ClaimProvenance.MODEL_KNOWLEDGE)
    synthesis = create_synthesis(
        conn, session_id, revision_id, SufficiencyState.PARTIAL, (claim_a, claim_b), "gpt-5",
        "en", T0)
    assert len(synthesis.claims) == 2
    assert synthesis.claims[0].citations[0].evidence_item_id == item_id
    assert synthesis.claims[1].citations == ()


def test_synthesis_citations_are_not_a_polymorphic_table(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(research_synthesis_citations)")}
    assert "parent_type" not in columns
    assert "synthesis_id" in columns
    assert "evidence_item_id" in columns


def test_get_synthesis_returns_none_for_missing_id(conn):
    assert get_synthesis(conn, 999) is None


def test_get_latest_synthesis_returns_highest_version(conn, scenario):
    session_id, revision_id, item_id, citation_number = scenario
    claim = _evidence_claim(item_id, citation_number)
    create_synthesis(
        conn, session_id, revision_id, SufficiencyState.PARTIAL, (claim,), "gpt-5", "en", T0)
    second = create_synthesis(
        conn, session_id, revision_id, SufficiencyState.SUFFICIENT, (claim,), "gpt-5", "en", T0)
    assert get_latest_synthesis(conn, session_id).id == second.id


def test_get_latest_synthesis_none_when_no_syntheses(conn, scenario):
    session_id, *_ = scenario
    assert get_latest_synthesis(conn, session_id) is None


def test_list_syntheses_ordered_by_version(conn, scenario):
    session_id, revision_id, item_id, citation_number = scenario
    claim = _evidence_claim(item_id, citation_number)
    create_synthesis(
        conn, session_id, revision_id, SufficiencyState.PARTIAL, (claim,), "gpt-5", "en", T0)
    create_synthesis(
        conn, session_id, revision_id, SufficiencyState.SUFFICIENT, (claim,), "gpt-5", "en", T0)
    versions = [s.version for s in list_syntheses(conn, session_id)]
    assert versions == [1, 2]


# ============================================================================
# create_synthesis_if_claimed: run-claim AND deadline fencing (Task C; mirrors
# finalize_snapshot_if_claimed's own claim+deadline fence)
# ============================================================================

def test_create_synthesis_if_claimed_persists_when_claim_is_active(conn, claimed_scenario):
    session_id, run_id, claim_token, revision_id, item_id, citation_number = claimed_scenario
    claim = _evidence_claim(item_id, citation_number)
    result = create_synthesis_if_claimed(
        conn, run_id, claim_token, session_id, revision_id, SufficiencyState.PARTIAL, (claim,),
        "gpt-5", "en", T0)
    assert result.ok
    assert result.failure_reason is None
    assert result.synthesis.version == 1
    assert result.synthesis.claims == (claim,)


def test_create_synthesis_if_claimed_requires_at_least_one_claim(conn, claimed_scenario):
    session_id, run_id, claim_token, revision_id, *_ = claimed_scenario
    with pytest.raises(ValueError, match="at least one claim"):
        create_synthesis_if_claimed(
            conn, run_id, claim_token, session_id, revision_id, SufficiencyState.PARTIAL, (),
            "gpt-5", "en", T0)


def test_create_synthesis_if_claimed_rejects_a_stale_claim_token(conn, claimed_scenario):
    session_id, run_id, claim_token, revision_id, item_id, citation_number = claimed_scenario
    claim = _evidence_claim(item_id, citation_number)
    result = create_synthesis_if_claimed(
        conn, run_id, "not-the-real-token", session_id, revision_id, SufficiencyState.PARTIAL,
        (claim,), "gpt-5", "en", T0)
    assert not result.ok
    assert result.failure_reason == SynthesisPersistFailureReason.CLAIM_LOST
    assert result.synthesis is None
    assert list_syntheses(conn, session_id) == []


def test_create_synthesis_if_claimed_rejects_once_run_is_completed(conn, claimed_scenario):
    session_id, run_id, claim_token, revision_id, item_id, citation_number = claimed_scenario
    claim = _evidence_claim(item_id, citation_number)
    assert complete_research_run(
        conn, run_id, claim_token, ResearchRunStatus.COMPLETED, T0)
    result = create_synthesis_if_claimed(
        conn, run_id, claim_token, session_id, revision_id, SufficiencyState.PARTIAL, (claim,),
        "gpt-5", "en", T0)
    assert not result.ok
    assert result.failure_reason == SynthesisPersistFailureReason.CLAIM_LOST
    assert result.synthesis is None
    assert list_syntheses(conn, session_id) == []
    # zero side effects: no citation rows either
    row = conn.execute("SELECT COUNT(*) AS n FROM research_synthesis_citations").fetchone()
    assert row["n"] == 0


def test_create_synthesis_if_claimed_rejects_a_foreign_session(conn, claimed_scenario):
    session_id, run_id, claim_token, revision_id, item_id, citation_number = claimed_scenario
    other_session_id = create_research_session(conn, "Other question", T0).id
    claim = _evidence_claim(item_id, citation_number)
    result = create_synthesis_if_claimed(
        conn, run_id, claim_token, other_session_id, revision_id, SufficiencyState.PARTIAL,
        (claim,), "gpt-5", "en", T0)
    assert not result.ok
    assert result.failure_reason == SynthesisPersistFailureReason.CLAIM_LOST
    assert list_syntheses(conn, other_session_id) == []


def test_create_synthesis_if_claimed_is_append_only_and_increments_version(conn, claimed_scenario):
    session_id, run_id, claim_token, revision_id, item_id, citation_number = claimed_scenario
    claim = _evidence_claim(item_id, citation_number)
    first = create_synthesis_if_claimed(
        conn, run_id, claim_token, session_id, revision_id, SufficiencyState.PARTIAL, (claim,),
        "gpt-5", "en", T0)
    second = create_synthesis_if_claimed(
        conn, run_id, claim_token, session_id, revision_id, SufficiencyState.SUFFICIENT,
        (claim,), "gpt-5", "en", T0 + timedelta(minutes=1))
    assert first.ok and second.ok
    assert second.synthesis.version == first.synthesis.version + 1
    assert get_synthesis(conn, first.synthesis.id).sufficiency_state == SufficiencyState.PARTIAL


# ============================================================================
# create_synthesis_if_claimed: deadline fencing (Task C)
# ============================================================================

def test_create_synthesis_if_claimed_fails_the_run_outright_when_the_deadline_has_arrived(
        conn, claimed_scenario):
    session_id, run_id, claim_token, revision_id, item_id, citation_number = claimed_scenario
    claim = _evidence_claim(item_id, citation_number)
    run_row = conn.execute(
        "SELECT deadline_at FROM research_runs WHERE id = ?", (run_id,)).fetchone()
    deadline_at = datetime.fromisoformat(run_row["deadline_at"])

    result = create_synthesis_if_claimed(
        conn, run_id, claim_token, session_id, revision_id, SufficiencyState.PARTIAL, (claim,),
        "gpt-5", "en", deadline_at)

    assert not result.ok
    assert result.failure_reason == SynthesisPersistFailureReason.DEADLINE_EXCEEDED
    assert result.synthesis is None
    assert list_syntheses(conn, session_id) == []
    row = conn.execute("SELECT COUNT(*) AS n FROM research_synthesis_citations").fetchone()
    assert row["n"] == 0

    run = get_research_run(conn, run_id)
    assert run.status == ResearchRunStatus.FAILED
    assert run.claim_token is None
    raw_row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run_id,)).fetchone()
    assert raw_row["error_code"] == "deadline_exceeded"


def test_create_synthesis_if_claimed_succeeds_one_instant_before_the_deadline(
        conn, claimed_scenario):
    session_id, run_id, claim_token, revision_id, item_id, citation_number = claimed_scenario
    claim = _evidence_claim(item_id, citation_number)
    run_row = conn.execute(
        "SELECT deadline_at FROM research_runs WHERE id = ?", (run_id,)).fetchone()
    just_before = datetime.fromisoformat(run_row["deadline_at"]) - timedelta(microseconds=1)

    result = create_synthesis_if_claimed(
        conn, run_id, claim_token, session_id, revision_id, SufficiencyState.PARTIAL, (claim,),
        "gpt-5", "en", just_before)

    assert result.ok
    assert get_research_run(conn, run_id).status == ResearchRunStatus.PROCESSING


def test_create_synthesis_if_claimed_uses_now_fn_over_pre_sampled_now(conn, claimed_scenario):
    """`now_fn`, when given, is authoritative: a call whose positional `now` looks well within
    budget must still fail the run once `now_fn()` reveals the deadline has arrived."""
    session_id, run_id, claim_token, revision_id, item_id, citation_number = claimed_scenario
    claim = _evidence_claim(item_id, citation_number)
    run_row = conn.execute(
        "SELECT deadline_at FROM research_runs WHERE id = ?", (run_id,)).fetchone()
    past_deadline = datetime.fromisoformat(run_row["deadline_at"]) + timedelta(seconds=1)

    result = create_synthesis_if_claimed(
        conn, run_id, claim_token, session_id, revision_id, SufficiencyState.PARTIAL, (claim,),
        "gpt-5", "en", T0, now_fn=lambda: past_deadline)

    assert not result.ok
    assert result.failure_reason == SynthesisPersistFailureReason.DEADLINE_EXCEEDED
    assert get_research_run(conn, run_id).status == ResearchRunStatus.FAILED


# ============================================================================
# create_synthesis_if_claimed: cancellation wins over the deadline check, but never fails the
# run (unlike DEADLINE_EXCEEDED/CLAIM_LOST)
# ============================================================================

def test_create_synthesis_if_claimed_discards_claims_when_cancelled_without_failing_the_run(
        conn, claimed_scenario):
    session_id, run_id, claim_token, revision_id, item_id, citation_number = claimed_scenario
    claim = _evidence_claim(item_id, citation_number)
    assert request_cancel_research_run(conn, run_id) is True

    result = create_synthesis_if_claimed(
        conn, run_id, claim_token, session_id, revision_id, SufficiencyState.PARTIAL, (claim,),
        "gpt-5", "en", T0)

    assert not result.ok
    assert result.failure_reason == SynthesisPersistFailureReason.CANCEL_REQUESTED
    assert result.synthesis is None
    assert list_syntheses(conn, session_id) == []
    row = conn.execute("SELECT COUNT(*) AS n FROM research_synthesis_citations").fetchone()
    assert row["n"] == 0

    # Unlike DEADLINE_EXCEEDED/CLAIM_LOST, the run is left exactly 'processing' -- its claim
    # remains good for the caller's own cancellation-aware terminal write.
    run = get_research_run(conn, run_id)
    assert run.status == ResearchRunStatus.PROCESSING
    assert run.claim_token == claim_token


def test_create_synthesis_if_claimed_cancellation_wins_over_an_already_arrived_deadline(
        conn, claimed_scenario):
    """Cancellation is checked BEFORE the deadline check -- a claim that is both cancelled AND
    past its own deadline must report CANCEL_REQUESTED (and leave the run 'processing'), never
    DEADLINE_EXCEEDED (which would instead fail the run outright)."""
    session_id, run_id, claim_token, revision_id, item_id, citation_number = claimed_scenario
    claim = _evidence_claim(item_id, citation_number)
    assert request_cancel_research_run(conn, run_id) is True
    run_row = conn.execute(
        "SELECT deadline_at FROM research_runs WHERE id = ?", (run_id,)).fetchone()
    past_deadline = datetime.fromisoformat(run_row["deadline_at"]) + timedelta(seconds=1)

    result = create_synthesis_if_claimed(
        conn, run_id, claim_token, session_id, revision_id, SufficiencyState.PARTIAL, (claim,),
        "gpt-5", "en", past_deadline)

    assert not result.ok
    assert result.failure_reason == SynthesisPersistFailureReason.CANCEL_REQUESTED
    assert get_research_run(conn, run_id).status == ResearchRunStatus.PROCESSING


# ============================================================================
# admit_synthesis_if_claimed: the atomic gate before any new Research Synthesis LLM call
# ============================================================================

def test_admit_synthesis_if_claimed_allows_an_active_claim_within_budget(conn, claimed_scenario):
    session_id, run_id, claim_token, *_ = claimed_scenario
    result = admit_synthesis_if_claimed(conn, run_id, claim_token, session_id, T0)
    assert result.allowed is True
    assert result.status == SynthesisAdmissionStatus.ALLOWED
    assert get_research_run(conn, run_id).status == ResearchRunStatus.PROCESSING


def test_admit_synthesis_if_claimed_rejects_a_stale_claim_token(conn, claimed_scenario):
    session_id, run_id, _claim_token, *_ = claimed_scenario
    result = admit_synthesis_if_claimed(conn, run_id, "not-the-real-token", session_id, T0)
    assert result.allowed is False
    assert result.status == SynthesisAdmissionStatus.CLAIM_LOST


def test_admit_synthesis_if_claimed_rejects_a_foreign_session(conn, claimed_scenario):
    session_id, run_id, claim_token, *_ = claimed_scenario
    other_session_id = create_research_session(conn, "Other question", T0).id
    result = admit_synthesis_if_claimed(conn, run_id, claim_token, other_session_id, T0)
    assert result.allowed is False
    assert result.status == SynthesisAdmissionStatus.CLAIM_LOST


def test_admit_synthesis_if_claimed_fails_the_run_outright_past_the_deadline(
        conn, claimed_scenario):
    session_id, run_id, claim_token, *_ = claimed_scenario
    run_row = conn.execute(
        "SELECT deadline_at FROM research_runs WHERE id = ?", (run_id,)).fetchone()
    deadline_at = datetime.fromisoformat(run_row["deadline_at"])

    result = admit_synthesis_if_claimed(conn, run_id, claim_token, session_id, deadline_at)

    assert result.allowed is False
    assert result.status == SynthesisAdmissionStatus.DEADLINE_EXCEEDED
    run = get_research_run(conn, run_id)
    assert run.status == ResearchRunStatus.FAILED
    assert run.claim_token is None
    raw_row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run_id,)).fetchone()
    assert raw_row["error_code"] == "deadline_exceeded"


def test_admit_synthesis_if_claimed_cancellation_wins_over_the_deadline_without_failing_the_run(
        conn, claimed_scenario):
    session_id, run_id, claim_token, *_ = claimed_scenario
    assert request_cancel_research_run(conn, run_id) is True
    run_row = conn.execute(
        "SELECT deadline_at FROM research_runs WHERE id = ?", (run_id,)).fetchone()
    past_deadline = datetime.fromisoformat(run_row["deadline_at"]) + timedelta(seconds=1)

    result = admit_synthesis_if_claimed(conn, run_id, claim_token, session_id, past_deadline)

    assert result.allowed is False
    assert result.status == SynthesisAdmissionStatus.CANCEL_REQUESTED
    run = get_research_run(conn, run_id)
    assert run.status == ResearchRunStatus.PROCESSING
    assert run.claim_token == claim_token


def test_admit_synthesis_if_claimed_uses_now_fn_over_pre_sampled_now(conn, claimed_scenario):
    session_id, run_id, claim_token, *_ = claimed_scenario
    run_row = conn.execute(
        "SELECT deadline_at FROM research_runs WHERE id = ?", (run_id,)).fetchone()
    past_deadline = datetime.fromisoformat(run_row["deadline_at"]) + timedelta(seconds=1)

    result = admit_synthesis_if_claimed(
        conn, run_id, claim_token, session_id, T0, now_fn=lambda: past_deadline)

    assert result.allowed is False
    assert result.status == SynthesisAdmissionStatus.DEADLINE_EXCEEDED
    assert get_research_run(conn, run_id).status == ResearchRunStatus.FAILED


def test_concurrent_create_synthesis_if_claimed_allocates_distinct_versions_no_gaps(
        tmp_path, conn, claimed_scenario):
    """Five threads across five independent connections race to persist a Research Synthesis
    for the SAME already-claimed run: the BEGIN IMMEDIATE version-allocation fence must still
    hand out five distinct, gapless versions (1..5), never a duplicate or a skipped number."""
    session_id, run_id, claim_token, revision_id, item_id, citation_number = claimed_scenario
    claim = _evidence_claim(item_id, citation_number)

    connections = [conn] + [_extra_connection(tmp_path) for _ in range(4)]
    barrier = threading.Barrier(5)
    results = {}
    errors = []

    def call(label, connection):
        barrier.wait(timeout=5)
        try:
            results[label] = create_synthesis_if_claimed(
                connection, run_id, claim_token, session_id, revision_id,
                SufficiencyState.PARTIAL, (claim,), "gpt-5", "en", T0)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=call, args=(i, connections[i])) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    for extra in connections[1:]:
        extra.close()

    assert errors == []
    assert all(r.ok for r in results.values())
    versions = sorted(r.synthesis.version for r in results.values())
    assert versions == [1, 2, 3, 4, 5]
