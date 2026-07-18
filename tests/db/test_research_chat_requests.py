from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.connection import connect, init_schema
from beehive.db.evidence_items import upsert_evidence_item
from beehive.db.evidence_state import (create_evidence_state_revision,
                                       get_latest_evidence_state_revision)
from beehive.db.research_chat_requests import (ChatRequestStatus,
                                                claim_chat_request,
                                                complete_chat_request_with_reply,
                                                enqueue_chat_request, fail_chat_request,
                                                get_active_chat_request, get_chat_request,
                                                heartbeat_chat_request,
                                                list_chat_requests,
                                                list_pending_chat_requests,
                                                recover_expired_chat_requests,
                                                requeue_chat_request,
                                                submit_chat_request)
from beehive.db.research_conversation_memory import (get_conversation_memory,
                                                       update_conversation_memory)
from beehive.db.research_messages import append_message, get_message, list_messages
from beehive.db.research_runs import claim_research_run, complete_research_run, enqueue_research_run
from beehive.db.research_sessions import create_research_session
from beehive.db.research_snapshots import create_snapshot, seal_snapshot
from beehive.db.research_sources import create_research_source
from beehive.db.research_syntheses import create_synthesis
from beehive.domain.research import (ClaimProvenance, ConversationRole, EvidenceCitation,
                                      EvidenceQuality, ResearchRunStatus, ResearchSourceOrigin,
                                      SufficiencyState, SynthesisClaim, SynthesisSection)

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


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
    owner_message = append_message(conn, session_id, ConversationRole.OWNER, "Why?", T0)
    return session_id, revision.id, owner_message.id, item.id, item.citation_number


@pytest.fixture
def synthesis_scenario(conn):
    """Like `scenario`, plus a Research Synthesis already persisted -- everything
    submit_chat_request requires before it will accept a chat turn. The Research Run is claimed
    and completed (a terminal state) so the session has no active run of its own, which would
    otherwise block archiving for a reason unrelated to what a given test means to exercise."""
    session_id = create_research_session(conn, "Q", T0).id
    source_id = create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0).id
    run_id = enqueue_research_run(conn, session_id, T0).id
    lease = claim_research_run(conn, run_id, T0, lease_seconds=600, deadline_seconds=3600)
    snapshot_id = create_snapshot(conn, session_id, run_id, T0).id
    item = upsert_evidence_item(
        conn, session_id, source_id, "e1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0)
    seal_snapshot(conn, snapshot_id, T0)
    revision = create_evidence_state_revision(conn, session_id, snapshot_id, [item.id], T0)
    claim = SynthesisClaim(
        text="Bottom line", section=SynthesisSection.BOTTOM_LINE,
        provenance=ClaimProvenance.EVIDENCE,
        citations=(EvidenceCitation(item.id, item.citation_number),))
    synthesis = create_synthesis(
        conn, session_id, revision.id, SufficiencyState.PARTIAL, (claim,), "gpt-5", "en", T0)
    complete_research_run(conn, run_id, lease.run.claim_token, ResearchRunStatus.COMPLETED, T0)
    return session_id, revision.id, synthesis.id, item.id, item.citation_number


def test_enqueue_chat_request_starts_pending_with_pinned_versions(conn, scenario):
    session_id, revision_id, owner_message_id, _, _ = scenario
    request = enqueue_chat_request(
        conn, session_id, owner_message_id, revision_id, None, 0, T0)
    assert request.status == ChatRequestStatus.PENDING
    assert request.pinned_evidence_state_revision_id == revision_id
    assert request.pinned_synthesis_id is None
    assert request.pinned_memory_version == 0


def test_enqueue_chat_request_rejects_second_active_request_for_same_session(conn, scenario):
    session_id, revision_id, owner_message_id, _, _ = scenario
    enqueue_chat_request(conn, session_id, owner_message_id, revision_id, None, 0, T0)
    second_owner_message = append_message(
        conn, session_id, ConversationRole.OWNER, "Again?", T0)
    with pytest.raises(ValueError, match="already has an active chat request"):
        enqueue_chat_request(
            conn, session_id, second_owner_message.id, revision_id, None, 0, T0)


def test_enqueue_chat_request_allowed_again_after_completion(conn, scenario):
    session_id, revision_id, owner_message_id, item_id, citation_number = scenario
    first = enqueue_chat_request(conn, session_id, owner_message_id, revision_id, None, 0, T0)
    claimed = claim_chat_request(conn, first.id, T0, lease_seconds=60)
    complete_chat_request_with_reply(
        conn, first.id, claimed.claim_token, session_id, owner_message_id, "Answer", (),
        "memory", None, T0)

    second_owner_message = append_message(
        conn, session_id, ConversationRole.OWNER, "Follow-up?", T0)
    second = enqueue_chat_request(
        conn, session_id, second_owner_message.id, revision_id, None, 0, T0)
    assert second.status == ChatRequestStatus.PENDING


def test_get_active_chat_request_none_when_no_requests(conn, scenario):
    session_id, *_ = scenario
    assert get_active_chat_request(conn, session_id) is None


def test_enqueue_chat_request_rejects_archived_session(conn):
    from beehive.db.research_sessions import archive_research_session

    session_id = create_research_session(conn, "Q", T0).id
    source_id = create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0).id
    run_id = enqueue_research_run(conn, session_id, T0).id
    claimed = claim_research_run(conn, run_id, T0, lease_seconds=60, deadline_seconds=3600)
    snapshot_id = create_snapshot(conn, session_id, run_id, T0).id
    item = upsert_evidence_item(
        conn, session_id, source_id, "e1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0)
    seal_snapshot(conn, snapshot_id, T0)
    revision = create_evidence_state_revision(conn, session_id, snapshot_id, [item.id], T0)
    complete_research_run(conn, run_id, claimed.run.claim_token, ResearchRunStatus.COMPLETED, T0)
    owner_message = append_message(conn, session_id, ConversationRole.OWNER, "Why?", T0)
    archive_research_session(conn, session_id, T0)

    with pytest.raises(ValueError, match="non-active Research Session"):
        enqueue_chat_request(conn, session_id, owner_message.id, revision.id, None, 0, T0)
    assert list_chat_requests(conn, session_id) == []


def test_claim_chat_request_sets_lease_and_claim_token(conn, scenario):
    session_id, revision_id, owner_message_id, _, _ = scenario
    request = enqueue_chat_request(
        conn, session_id, owner_message_id, revision_id, None, 0, T0)
    claimed = claim_chat_request(conn, request.id, T0, lease_seconds=60)
    assert claimed.status == ChatRequestStatus.PROCESSING
    assert claimed.claim_token is not None
    assert claimed.started_at == T0.isoformat()


def test_claim_chat_request_fails_if_already_claimed(conn, scenario):
    session_id, revision_id, owner_message_id, _, _ = scenario
    request = enqueue_chat_request(
        conn, session_id, owner_message_id, revision_id, None, 0, T0)
    claim_chat_request(conn, request.id, T0, lease_seconds=60)
    assert claim_chat_request(conn, request.id, T0, lease_seconds=60) is None


def test_heartbeat_chat_request_extends_lease_for_matching_claim(conn, scenario):
    session_id, revision_id, owner_message_id, _, _ = scenario
    request = enqueue_chat_request(
        conn, session_id, owner_message_id, revision_id, None, 0, T0)
    claimed = claim_chat_request(conn, request.id, T0, lease_seconds=60)
    ok = heartbeat_chat_request(
        conn, request.id, claimed.claim_token, T0 + timedelta(seconds=10), lease_seconds=60)
    assert ok is True
    assert heartbeat_chat_request(
        conn, request.id, "wrong-token", T0, lease_seconds=60) is False


def test_requeue_chat_request_gives_back_a_still_valid_claim(conn, scenario):
    session_id, revision_id, owner_message_id, _, _ = scenario
    request = enqueue_chat_request(
        conn, session_id, owner_message_id, revision_id, None, 0, T0)
    claimed = claim_chat_request(conn, request.id, T0, lease_seconds=60)

    ok = requeue_chat_request(conn, request.id, claimed.claim_token)
    assert ok is True
    requeued = get_chat_request(conn, request.id)
    assert requeued.status == ChatRequestStatus.PENDING
    assert requeued.claim_token is None
    assert requeued.lease_expires_at is None
    # Requeuing is a continuation, not a fresh submission: started_at is left untouched.
    assert requeued.started_at == claimed.started_at


def test_requeue_chat_request_rejects_a_stale_or_wrong_claim_token(conn, scenario):
    session_id, revision_id, owner_message_id, _, _ = scenario
    request = enqueue_chat_request(
        conn, session_id, owner_message_id, revision_id, None, 0, T0)
    claim_chat_request(conn, request.id, T0, lease_seconds=60)

    ok = requeue_chat_request(conn, request.id, "wrong-token")
    assert ok is False
    assert get_chat_request(conn, request.id).status == ChatRequestStatus.PROCESSING


def test_requeue_chat_request_rejects_an_already_terminal_request(conn, scenario):
    session_id, revision_id, owner_message_id, _, _ = scenario
    request = enqueue_chat_request(
        conn, session_id, owner_message_id, revision_id, None, 0, T0)
    claimed = claim_chat_request(conn, request.id, T0, lease_seconds=60)
    fail_chat_request(conn, request.id, claimed.claim_token, "model_error", "timed out", T0)

    ok = requeue_chat_request(conn, request.id, claimed.claim_token)
    assert ok is False
    assert get_chat_request(conn, request.id).status == ChatRequestStatus.FAILED


def test_fail_chat_request_clears_lease_and_sets_error(conn, scenario):
    session_id, revision_id, owner_message_id, _, _ = scenario
    request = enqueue_chat_request(
        conn, session_id, owner_message_id, revision_id, None, 0, T0)
    claimed = claim_chat_request(conn, request.id, T0, lease_seconds=60)
    ok = fail_chat_request(
        conn, request.id, claimed.claim_token, "model_error", "timed out", T0)
    assert ok is True
    final = get_chat_request(conn, request.id)
    assert final.status == ChatRequestStatus.FAILED
    assert final.claim_token is None

    # a session with a failed chat request is no longer "active", so a new one can be enqueued
    second_owner_message = append_message(
        conn, session_id, ConversationRole.OWNER, "Retry?", T0)
    enqueue_chat_request(
        conn, session_id, second_owner_message.id, revision_id, None, 0, T0)


def test_recover_expired_chat_requests_requeues_expired_leases(conn, scenario):
    session_id, revision_id, owner_message_id, _, _ = scenario
    request = enqueue_chat_request(
        conn, session_id, owner_message_id, revision_id, None, 0, T0)
    claim_chat_request(conn, request.id, T0, lease_seconds=10)
    count = recover_expired_chat_requests(conn, T0 + timedelta(seconds=30))
    assert count == 1
    assert get_chat_request(conn, request.id).status == ChatRequestStatus.PENDING


def test_list_pending_chat_requests_orders_oldest_first(conn, scenario):
    session_id, revision_id, owner_message_id, _, _ = scenario
    first = enqueue_chat_request(
        conn, session_id, owner_message_id, revision_id, None, 0, T0)
    pending = list_pending_chat_requests(conn)
    assert [r.id for r in pending] == [first.id]


def test_list_chat_requests_scoped_to_session(conn, scenario):
    session_id, revision_id, owner_message_id, _, _ = scenario
    request = enqueue_chat_request(
        conn, session_id, owner_message_id, revision_id, None, 0, T0)
    assert [r.id for r in list_chat_requests(conn, session_id)] == [request.id]


# -- atomic reply + memory completion ----------------------------------------------------

def test_complete_chat_request_with_reply_writes_reply_completes_and_bumps_memory(
        conn, scenario):
    session_id, revision_id, owner_message_id, item_id, citation_number = scenario
    request = enqueue_chat_request(
        conn, session_id, owner_message_id, revision_id, None, 0, T0)
    claimed = claim_chat_request(conn, request.id, T0, lease_seconds=60)

    citation = EvidenceCitation(evidence_item_id=item_id, citation_number=citation_number)
    result = complete_chat_request_with_reply(
        conn, request.id, claimed.claim_token, session_id, owner_message_id,
        "Because of X [1]", (citation,), "memory v1", owner_message_id,
        T0 + timedelta(seconds=5))

    assert result is not None
    completed_request, reply_message = result
    assert completed_request.status == ChatRequestStatus.COMPLETED
    assert completed_request.reply_message_id == reply_message.id
    assert completed_request.claim_token is None
    assert reply_message.content == "Because of X [1]"

    memory = get_conversation_memory(conn, session_id)
    assert memory.version == 1
    assert memory.content == "memory v1"
    assert memory.covers_through_message_id == owner_message_id

    assert get_message(conn, reply_message.id) is not None


def test_complete_chat_request_with_reply_rejects_stale_claim(conn, scenario):
    session_id, revision_id, owner_message_id, _, _ = scenario
    request = enqueue_chat_request(
        conn, session_id, owner_message_id, revision_id, None, 0, T0)
    claim_chat_request(conn, request.id, T0, lease_seconds=60)

    result = complete_chat_request_with_reply(
        conn, request.id, "wrong-token", session_id, owner_message_id, "Answer", (), "memory",
        None, T0)
    assert result is None
    # nothing was written: request is still processing, no memory row was created
    assert get_chat_request(conn, request.id).status == ChatRequestStatus.PROCESSING
    assert get_conversation_memory(conn, session_id) is None


def test_complete_chat_request_with_reply_second_reply_bumps_memory_version(conn, scenario):
    session_id, revision_id, owner_message_id, _, _ = scenario
    first = enqueue_chat_request(
        conn, session_id, owner_message_id, revision_id, None, 0, T0)
    claimed = claim_chat_request(conn, first.id, T0, lease_seconds=60)
    complete_chat_request_with_reply(
        conn, first.id, claimed.claim_token, session_id, owner_message_id, "First answer", (),
        "memory v1", None, T0)

    second_owner_message = append_message(
        conn, session_id, ConversationRole.OWNER, "More?", T0)
    second = enqueue_chat_request(
        conn, session_id, second_owner_message.id, revision_id, None, 1, T0)
    claimed2 = claim_chat_request(conn, second.id, T0, lease_seconds=60)
    complete_chat_request_with_reply(
        conn, second.id, claimed2.claim_token, session_id, second_owner_message.id,
        "Second answer", (), "memory v2", second_owner_message.id, T0 + timedelta(seconds=1))

    memory = get_conversation_memory(conn, session_id)
    assert memory.version == 2
    assert memory.content == "memory v2"


def test_complete_chat_request_with_reply_rejects_session_id_mismatch(conn, scenario):
    session_id, revision_id, owner_message_id, _, _ = scenario
    request = enqueue_chat_request(
        conn, session_id, owner_message_id, revision_id, None, 0, T0)
    claimed = claim_chat_request(conn, request.id, T0, lease_seconds=60)
    other_session_id = create_research_session(conn, "Other", T0).id

    with pytest.raises(ValueError, match="belongs to Research Session"):
        complete_chat_request_with_reply(
            conn, request.id, claimed.claim_token, other_session_id, owner_message_id, "Answer",
            (), "memory", None, T0)
    # nothing was written: request is still processing, no reply message, no memory row
    assert get_chat_request(conn, request.id).status == ChatRequestStatus.PROCESSING
    assert get_conversation_memory(conn, session_id) is None


def test_complete_chat_request_with_reply_rejects_owner_message_id_mismatch(conn, scenario):
    session_id, revision_id, owner_message_id, _, _ = scenario
    request = enqueue_chat_request(
        conn, session_id, owner_message_id, revision_id, None, 0, T0)
    claimed = claim_chat_request(conn, request.id, T0, lease_seconds=60)
    other_owner_message = append_message(conn, session_id, ConversationRole.OWNER, "Other?", T0)

    with pytest.raises(ValueError, match="pinned to owner message"):
        complete_chat_request_with_reply(
            conn, request.id, claimed.claim_token, session_id, other_owner_message.id, "Answer",
            (), "memory", None, T0)
    assert get_chat_request(conn, request.id).status == ChatRequestStatus.PROCESSING
    assert get_conversation_memory(conn, session_id) is None


def test_complete_chat_request_with_reply_rejects_stale_memory_pin(conn, scenario):
    session_id, revision_id, owner_message_id, _, _ = scenario
    request = enqueue_chat_request(
        conn, session_id, owner_message_id, revision_id, None, 0, T0)
    claimed = claim_chat_request(conn, request.id, T0, lease_seconds=60)
    # Something else bumps the session's Conversation Memory after this request was pinned at
    # version 0 -- should be impossible given the one-pending-request-per-session invariant,
    # but complete_chat_request_with_reply must still reject rather than silently clobber it.
    update_conversation_memory(conn, session_id, "external bump", None, T0)

    with pytest.raises(ValueError, match="Conversation Memory version"):
        complete_chat_request_with_reply(
            conn, request.id, claimed.claim_token, session_id, owner_message_id, "Answer", (),
            "memory", None, T0)
    assert get_chat_request(conn, request.id).status == ChatRequestStatus.PROCESSING
    # the external bump itself is untouched -- no second bump, no reply written
    memory = get_conversation_memory(conn, session_id)
    assert memory.version == 1
    assert memory.content == "external bump"


# ============================================================================
# submit_chat_request: one atomic owner-message + chat-request transaction
# ============================================================================

def test_submit_chat_request_inserts_message_and_pins_current_state(conn, synthesis_scenario):
    session_id, revision_id, synthesis_id, _, _ = synthesis_scenario
    message, request = submit_chat_request(conn, session_id, "Follow-up?", T0)

    assert message.role == ConversationRole.OWNER
    assert message.content == "Follow-up?"
    assert request.owner_message_id == message.id
    assert request.status == ChatRequestStatus.PENDING
    assert request.pinned_evidence_state_revision_id == revision_id
    assert request.pinned_synthesis_id == synthesis_id
    assert request.pinned_memory_version == 0


def test_submit_chat_request_pins_current_memory_version(conn, synthesis_scenario):
    session_id, *_ = synthesis_scenario
    update_conversation_memory(conn, session_id, "existing", None, T0)
    _message, request = submit_chat_request(conn, session_id, "Follow-up?", T0)
    assert request.pinned_memory_version == 1


def test_submit_chat_request_rejects_missing_evidence_state_revision(conn):
    session_id = create_research_session(conn, "Q", T0).id
    with pytest.raises(ValueError, match="no Evidence State Revision"):
        submit_chat_request(conn, session_id, "First question?", T0)
    assert list_messages(conn, session_id) == []


def test_submit_chat_request_rejects_missing_synthesis(conn, scenario):
    session_id, *_ = scenario  # `scenario` has an Evidence State Revision but no synthesis
    with pytest.raises(ValueError, match="no Research Synthesis"):
        submit_chat_request(conn, session_id, "First question?", T0)
    # zero rows written: no orphan owner message left behind by the rejected submission
    assert list_messages(conn, session_id) == [get_message(conn, scenario[2])]


def test_submit_chat_request_rejects_empty_latest_evidence_revision(
        conn, synthesis_scenario):
    session_id, *_ = synthesis_scenario
    current_revision = get_latest_evidence_state_revision(conn, session_id)
    create_evidence_state_revision(
        conn, session_id, current_revision.snapshot_id, [], T0 + timedelta(minutes=1))

    with pytest.raises(ValueError, match="no active Evidence Items"):
        submit_chat_request(conn, session_id, "Question with no citable evidence?", T0)

    assert list_messages(conn, session_id) == []
    assert list_chat_requests(conn, session_id) == []


def test_submit_chat_request_rejects_second_active_request(conn, synthesis_scenario):
    session_id, *_ = synthesis_scenario
    submit_chat_request(conn, session_id, "Q1?", T0)
    with pytest.raises(ValueError, match="active chat request"):
        submit_chat_request(conn, session_id, "Q2?", T0)
    # the rejected second submission never wrote its own owner message
    assert len(list_messages(conn, session_id)) == 1


def test_submit_chat_request_rejects_archived_session(conn, synthesis_scenario):
    from beehive.db.research_sessions import archive_research_session

    session_id, _revision_id, _synthesis_id, _, _ = synthesis_scenario
    archive_research_session(conn, session_id, T0)

    with pytest.raises(ValueError, match="non-active Research Session"):
        submit_chat_request(conn, session_id, "Q?", T0)
    assert list_messages(conn, session_id) == []


def test_submit_chat_request_allowed_again_after_completion(conn, synthesis_scenario):
    session_id, *_ = synthesis_scenario
    first_message, first_request = submit_chat_request(conn, session_id, "Q1?", T0)
    claimed = claim_chat_request(conn, first_request.id, T0, lease_seconds=60)
    complete_chat_request_with_reply(
        conn, first_request.id, claimed.claim_token, session_id, first_message.id, "Answer", (),
        "memory", None, T0)

    _second_message, second_request = submit_chat_request(conn, session_id, "Q2?", T0)
    assert second_request.status == ChatRequestStatus.PENDING
    # Q1 (owner) + reply (assistant) + Q2 (owner)
    assert len(list_messages(conn, session_id)) == 3
