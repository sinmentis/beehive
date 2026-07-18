"""Narrowly-scoped integrity tests for the Research persistence schema itself (ADR-0006..0010):
that every research_* table participates correctly in ON DELETE CASCADE, that the schema's own
CHECK constraints and partial unique indexes reject invalid rows even when a caller bypasses
the repository modules and writes raw SQL, and that a full lifecycle followed by a hard delete
leaves no dangling foreign keys or orphaned rows behind."""
from datetime import datetime, timezone

import pytest
import sqlite3

from beehive.db.connection import connect, init_schema
from beehive.db.evidence_clusters import create_evidence_cluster
from beehive.db.evidence_curation import set_evidence_curation
from beehive.db.evidence_items import upsert_evidence_item
from beehive.db.evidence_state import create_evidence_state_revision
from beehive.db.research_chat_requests import (claim_chat_request,
                                                complete_chat_request_with_reply,
                                                enqueue_chat_request)
from beehive.db.research_messages import append_message
from beehive.db.research_plan_revisions import create_plan_revision
from beehive.db.research_runs import claim_research_run, enqueue_research_run
from beehive.db.research_sessions import create_research_session, hard_delete_research_session
from beehive.db.research_snapshots import add_snapshot_items, create_snapshot, seal_snapshot
from beehive.db.research_sources import create_research_source
from beehive.db.research_syntheses import create_synthesis
from beehive.domain.research import (ClaimProvenance, ConversationRole, EvidenceCitation,
                                      EvidenceQuality, ResearchSourceOrigin, SufficiencyState,
                                      SynthesisClaim, SynthesisSection)

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)

_RESEARCH_TABLES = [
    "research_sessions", "research_sources", "research_runs", "research_plan_revisions",
    "research_evidence_items", "research_snapshots", "research_snapshot_items",
    "research_evidence_curation", "research_evidence_state_revisions",
    "research_evidence_state_revision_items", "research_evidence_clusters",
    "research_evidence_cluster_items", "research_syntheses", "research_synthesis_citations",
    "research_messages", "research_message_citations", "research_chat_requests",
    "research_conversation_memory",
]


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


def test_fresh_schema_has_no_foreign_key_violations(conn):
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_full_lifecycle_then_hard_delete_leaves_no_orphans(conn):
    session = create_research_session(conn, "What changed?", T0)
    source = create_research_source(
        conn, session.id, "web_search", {}, ResearchSourceOrigin.OWNER, T0)
    run = enqueue_research_run(conn, session.id, T0)
    claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)
    create_plan_revision(conn, run.id, '{"queries":[]}', "plan", True, T0)

    item = upsert_evidence_item(
        conn, session.id, source.id, "e1", "T1", "https://x/1", EvidenceQuality.REPORTING, T0)
    snapshot = create_snapshot(conn, session.id, run.id, T0)
    add_snapshot_items(conn, snapshot.id, [item.id], T0)
    create_evidence_cluster(conn, snapshot.id, [item.id], T0)
    seal_snapshot(conn, snapshot.id, T0)
    set_evidence_curation(conn, item.id, False, "looks good", T0)
    revision = create_evidence_state_revision(conn, session.id, snapshot.id, [item.id], T0)

    claim = SynthesisClaim(
        text="Claim", section=SynthesisSection.BOTTOM_LINE, provenance=ClaimProvenance.EVIDENCE,
        citations=(EvidenceCitation(item.id, item.citation_number),))
    synthesis = create_synthesis(
        conn, session.id, revision.id, SufficiencyState.PARTIAL, (claim,), "gpt-5", "en", T0)

    owner_message = append_message(conn, session.id, ConversationRole.OWNER, "Why?", T0)
    chat_request = enqueue_chat_request(
        conn, session.id, owner_message.id, revision.id, synthesis.id, 0, T0)
    chat_claimed = claim_chat_request(conn, chat_request.id, T0, lease_seconds=60)
    complete_chat_request_with_reply(
        conn, chat_request.id, chat_claimed.claim_token, session.id, owner_message.id,
        "Answer [1]", (EvidenceCitation(item.id, item.citation_number),), "memory",
        owner_message.id, T0)

    hard_delete_research_session(conn, session.id)

    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    for table in _RESEARCH_TABLES:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert count == 0, f"{table} still has rows after hard-deleting its owning session"


def test_processing_run_requires_phase_claim_and_lease(conn):
    session_id = create_research_session(conn, "Q", T0).id
    conn.execute(
        "INSERT INTO research_runs (session_id, status, requested_at) "
        "VALUES (?, 'pending', ?)", (session_id, T0.isoformat()))
    conn.commit()
    run_id = conn.execute("SELECT id FROM research_runs").fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE research_runs SET status = 'processing' WHERE id = ?", (run_id,))


def test_terminal_run_requires_completed_at(conn):
    session_id = create_research_session(conn, "Q", T0).id
    conn.execute(
        "INSERT INTO research_runs (session_id, status, requested_at) "
        "VALUES (?, 'pending', ?)", (session_id, T0.isoformat()))
    run_id = conn.execute("SELECT id FROM research_runs").fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE research_runs SET status = 'completed' WHERE id = ?", (run_id,))


def test_citation_number_is_unique_per_session(conn):
    session_id = create_research_session(conn, "Q", T0).id
    source_id = create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0).id
    upsert_evidence_item(
        conn, session_id, source_id, "e1", "T1", "https://x/1", EvidenceQuality.REPORTING, T0)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO research_evidence_items (session_id, research_source_id, "
            "external_key, title, url, quality, citation_number, created_at) "
            "VALUES (?, ?, 'e2', 'T2', 'https://x/2', 'reporting', 1, ?)",
            (session_id, source_id, T0.isoformat()))


def test_at_most_one_active_chat_request_per_session_is_index_enforced(conn):
    session_id = create_research_session(conn, "Q", T0).id
    create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0)
    run_id = enqueue_research_run(conn, session_id, T0).id
    snapshot_id = create_snapshot(conn, session_id, run_id, T0).id
    seal_snapshot(conn, snapshot_id, T0)
    revision = create_evidence_state_revision(conn, session_id, snapshot_id, [], T0)
    message_a = append_message(conn, session_id, ConversationRole.OWNER, "Q1", T0)
    message_b = append_message(conn, session_id, ConversationRole.OWNER, "Q2", T0)

    conn.execute(
        "INSERT INTO research_chat_requests (session_id, owner_message_id, status, "
        "pinned_evidence_state_revision_id, pinned_memory_version, requested_at) "
        "VALUES (?, ?, 'pending', ?, 0, ?)",
        (session_id, message_a.id, revision.id, T0.isoformat()))
    conn.commit()

    # bypassing enqueue_chat_request's application-level check entirely: the partial unique
    # index itself must still reject a second pending/processing row for the same session.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO research_chat_requests (session_id, owner_message_id, status, "
            "pinned_evidence_state_revision_id, pinned_memory_version, requested_at) "
            "VALUES (?, ?, 'pending', ?, 0, ?)",
            (session_id, message_b.id, revision.id, T0.isoformat()))


def test_evidence_source_and_external_key_pair_is_unique(conn):
    session_id = create_research_session(conn, "Q", T0).id
    source_id = create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0).id
    upsert_evidence_item(
        conn, session_id, source_id, "dup", "T1", "https://x/1", EvidenceQuality.REPORTING, T0)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO research_evidence_items (session_id, research_source_id, "
            "external_key, title, url, quality, citation_number, created_at) "
            "VALUES (?, ?, 'dup', 'T2', 'https://x/2', 'reporting', 2, ?)",
            (session_id, source_id, T0.isoformat()))


def test_at_most_one_evidence_snapshot_per_run_is_index_enforced(conn):
    """Task C's domain invariant: exactly one research_snapshots row per research_run. This is
    already enforced at the repository level (db.research_snapshots.create_snapshot raises
    ValueError for a second snapshot against the same run_id), but bypassing that application
    check entirely -- raw SQL straight against the table -- must still be rejected by the
    schema's own UNIQUE(run_id) index (idx_research_snapshots_one_per_run), exactly like the
    one-active-chat-request-per-session index above."""
    session_id = create_research_session(conn, "Q", T0).id
    run_id = enqueue_research_run(conn, session_id, T0).id
    create_snapshot(conn, session_id, run_id, T0)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO research_snapshots (session_id, run_id, sequence_number, status, "
            "created_at) VALUES (?, ?, 2, 'building', ?)",
            (session_id, run_id, T0.isoformat()))
