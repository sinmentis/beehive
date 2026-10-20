# tests/db/test_civic_schema_integrity.py
"""Integrity tests for the Beehive Civic schema itself: that every civic_* table participates
correctly in ON DELETE CASCADE, that the schema's own CHECK constraints and partial unique
indexes reject invalid rows even when a caller bypasses the repository Modules and writes raw
SQL, that the family is genuinely additive (nothing above it changed), and that an upgraded
database ends up identical to a fresh one.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from beehive.civic.ai_gateway import CivicAiStage
from beehive.db.civic_ai_stages import record_ai_stage_success, reserve_ai_stage
from beehive.db.civic_batches import (bind_batch_payload_hash, claim_batch_for_delivery,
                                       get_frozen_publication, get_or_create_batch_if_claimed,
                                       list_publication_rejections, seal_batch_if_claimed)
from beehive.db.civic_candidates import (add_candidate_evidence, create_candidate,
                                          mark_candidate_verified, set_candidate_status)
from beehive.civic.evidence import (CivicCandidateClaim, CivicClaimCategory,
                                    CivicContentClass, CivicEvidenceItem,
                                    CivicOwnershipRegistry, CivicPublisher, CivicPublisherKind,
                                    CivicRecordKind, CivicRecordOperation, CivicSourceKind,
                                    qualify_candidate)
from beehive.civic.pipeline import CIVIC_ENGINE_VERSION
from beehive.db.civic_consumed_items import claim_consumed_item
from beehive.db.civic_notifications import queue_notification
from beehive.db.civic_qualifications import record_qualification
from beehive.db.civic_receipts import record_delivery_receipt
from beehive.db.civic_runs import claim_civic_run, enqueue_civic_run
from beehive.db.civic_verifications import (record_research_attempt, record_verification,
                                             reserve_research_attempt)
from beehive.db.civic_vocabulary import (add_vocabulary_term, create_vocabulary_version,
                                          publish_vocabulary_version)
from beehive.db.civic_watches import (create_civic_watch, hard_delete_civic_watch,
                                       retire_civic_watch)
from beehive.db.connection import _SCHEMA_PATH, connect, init_schema
from beehive.domain.civic import (CivicCandidateStatus, CivicDeliveryChannel,
                                   CivicEvidenceStance, CivicEvidenceTier, CivicResearchOutcome,
                                   CivicTermCategory, CivicTermRole, CivicVerificationVerdict)

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)

_GENERIC_CIVIC_TABLES = [
    "civic_watches", "civic_vocabulary_versions", "civic_vocabulary_terms", "civic_runs",
    "civic_batches", "civic_candidates", "civic_candidate_evidence", "civic_capacity_deferrals",
    "civic_consumed_items", "civic_qualifications", "civic_research_attempts",
    "civic_verifications", "civic_notifications", "civic_delivery_receipts",
    "civic_ai_stages",
]
_PUBLICATION_TABLES = ["civic_batch_publications", "civic_publication_rejections"]
_CAPTURE_TABLES = ["civic_evidence_captures", "civic_hydration_attempts"]
# Watch-owned tables cascade with their Watch; source state outlives any one Watch.
_WATCH_OWNED_TABLES = [*_GENERIC_CIVIC_TABLES, *_PUBLICATION_TABLES, *_CAPTURE_TABLES]
_SOURCE_STATE_TABLES = ["civic_source_outages"]
_CIVIC_TABLES = [*_WATCH_OWNED_TABLES, *_SOURCE_STATE_TABLES]


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


def _qualification(candidate_id: int):
    """One real engine decision, so the receipt in _full_lifecycle is a decision the policy
    actually reached rather than a hand-written row the CHECK constraints happen to accept."""
    claim = CivicCandidateClaim(
        candidate_id=str(candidate_id), fingerprint="dk-1", record_kind=CivicRecordKind.TIMELINE,
        operation=CivicRecordOperation.ADD, subject_id="green-party", record_key="dk-1",
        event_date=T0.date(), category=CivicClaimCategory.PARTY_TIMELINE_EVENT,
        content_class=CivicContentClass.FACTUAL, durable=True, claim_text="A durable event")
    item = CivicEvidenceItem(
        evidence_id="e1", publisher_id="beehive", source_kind=CivicSourceKind.GOVERNMENT,
        evidence_tier=CivicEvidenceTier.OFFICIAL, stance=CivicEvidenceStance.SUPPORTING,
        canonical_url="https://example.test/1", title="Release", subject_id="green-party",
        event_date=T0.date(), category=CivicClaimCategory.PARTY_TIMELINE_EVENT, published_at=T0,
        accessible_full_text=True, paywalled=False, anonymous_source_dependency=False)
    registry = CivicOwnershipRegistry.build([
        CivicPublisher(
            publisher_id="beehive", name="Beehive", kind=CivicPublisherKind.OFFICIAL,
            source_kind=CivicSourceKind.GOVERNMENT)])
    return qualify_candidate(claim, [item], registry=registry)


def _full_lifecycle(conn) -> int:
    """Build a generic Civic Watch subtree covering the non-publication tables."""
    watch_id = create_civic_watch(conn, "NZ election", "What changed today?", T0).id
    version = create_vocabulary_version(conn, watch_id, T0)
    add_vocabulary_term(
        conn, version.id, "Green Party", CivicTermCategory.PARTY, CivicTermRole.REQUIRED, T0)
    publish_vocabulary_version(conn, version.id, T0)

    run = enqueue_civic_run(conn, watch_id, T0, T0, vocabulary_version_id=version.id)
    lease = claim_civic_run(conn, run.id, T0, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, T0)

    candidate = create_candidate(conn, batch.id, "dk-1", "Title", "Statement", T0)
    add_candidate_evidence(
        conn, candidate.id, "official_feed", "e1", "https://example.test/1", "Release",
        CivicEvidenceTier.OFFICIAL, CivicEvidenceStance.SUPPORTING, T0, publisher="Beehive")
    conn.execute("BEGIN IMMEDIATE")
    claim_consumed_item(
        conn, watch_id=watch_id, run_id=run.id, candidate_id=candidate.id,
        item_key="nz_beehive_releases:1", source_key="nz_beehive_releases", external_key="1",
        canonical_url="https://example.test/1", now=T0)
    record_qualification(
        conn, candidate_id=candidate.id, run_id=run.id,
        qualification=_qualification(candidate.id), engine_version=CIVIC_ENGINE_VERSION,
        input_hash="a" * 64, output_hash="b" * 64, research_attempts_used=0, now=T0)
    conn.commit()

    attempt = reserve_research_attempt(conn, candidate.id)
    record_research_attempt(
        conn, candidate.id, attempt, CivicResearchOutcome.RESOLVED, T0,
        T0 + timedelta(minutes=1), evidence_added=1)
    set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, T0)
    # One AI stage receipt, written the way the pipeline writes it: reserved under the Run's
    # claim, then settled with the validated output.
    reservation = reserve_ai_stage(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=lease.run.claim_token,
        stage=CivicAiStage.GENERATE, provider="github-copilot", model="claude-haiku-4.5",
        prompt_version="civic-ai-prompts/1", prompt_hash="p" * 64,
        schema_version="civic-ai-schema/1", input_hash="i" * 64, now=T0)
    record_ai_stage_success(
        conn, reservation=reservation,
        output={"decision": "qualify", "editorial_exclusion": None, "reason": "ok"}, now=T0)
    record_verification(conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", T0)
    mark_candidate_verified(conn, candidate.id, T0)

    # Seven verified Candidates against the normal cap of six, so the seal has to carry one
    # forward and civic_capacity_deferrals is exercised by the same lifecycle as everything else.
    for index in range(2, 9):
        extra = create_candidate(conn, batch.id, f"dk-{index}", f"Title {index}", "S", T0)
        add_candidate_evidence(
            conn, extra.id, "official_feed", f"e{index}", f"https://example.test/{index}",
            "Release", CivicEvidenceTier.OFFICIAL, CivicEvidenceStance.SUPPORTING, T0,
            publisher="Beehive")
        set_candidate_status(conn, extra.id, CivicCandidateStatus.VERIFYING, T0)
        record_verification(conn, extra.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", T0)
        mark_candidate_verified(conn, extra.id, T0)

    sealed = seal_batch_if_claimed(conn, batch.id, run.id, lease.run.claim_token, T0)
    sealed = bind_batch_payload_hash(conn, sealed.id, "f" * 64, T0)
    delivery = claim_batch_for_delivery(conn, sealed.id, T0, 60)
    record_delivery_receipt(
        conn, sealed.id, delivery.batch.claim_token, CivicDeliveryChannel.SPOOL, "/spool", T0)
    queue_notification(conn, watch_id, "run_completed", f"run-{run.id}", "Done", T0,
                       run_id=run.id)
    return watch_id


# ============================================================================
# The family is additive
# ============================================================================


def test_fresh_schema_has_no_foreign_key_violations(conn):
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_every_civic_table_exists(conn):
    present = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'civic_%'")}
    assert set(_CIVIC_TABLES) == present


def test_payload_hash_columns_are_added_to_a_legacy_civic_schema(conn):
    conn.execute("DROP TRIGGER civic_delivery_receipts_require_payload_hash")
    conn.execute("ALTER TABLE civic_delivery_receipts DROP COLUMN payload_hash")
    conn.execute("ALTER TABLE civic_batches DROP COLUMN payload_hash")
    conn.execute("PRAGMA user_version = 4")
    conn.commit()

    init_schema(conn)

    batch_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(civic_batches)")}
    receipt_columns = {
        row["name"] for row in conn.execute(
            "PRAGMA table_info(civic_delivery_receipts)")}
    assert "payload_hash" in batch_columns
    assert "payload_hash" in receipt_columns
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'trigger' "
        "AND name = 'civic_delivery_receipts_require_payload_hash'").fetchone()


def test_no_civic_table_references_the_feed_or_research_families(conn):
    """Civic is a separate, additive family: a Civic Watch never becomes a Channel/Source/Item or
    a Research Session, so no civic_* table may point at either."""
    for table in _CIVIC_TABLES:
        for row in conn.execute(f"PRAGMA foreign_key_list({table})"):
            assert row["table"].startswith("civic_"), (
                f"{table} references {row['table']}, which is outside the civic_* family")


def test_no_other_table_references_a_civic_table(conn):
    """The other direction: adding Civic must not have changed the feed or Research families."""
    tables = [row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'civic_%' "
        "AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        for row in conn.execute(f"PRAGMA foreign_key_list({table})"):
            assert not row["table"].startswith("civic_"), (
                f"{table} references civic table {row['table']}")


def test_the_civic_family_is_created_by_idempotent_statements(conn):
    """init_schema runs on every process start, so re-running it must be a no-op."""
    watch_id = _full_lifecycle(conn)
    init_schema(conn)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM civic_watches WHERE id = ?", (watch_id,)).fetchone()["n"] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_every_civic_create_statement_is_if_not_exists():
    """Guards the upgrade path: a database that predates Civic picks the whole family up from
    schema.sql without any column backfill, and one that already has it is untouched."""
    sql = _SCHEMA_PATH.read_text()
    civic_section = sql[sql.index("-- Beehive Civic persistence."):]
    for statement in civic_section.split(";"):
        stripped = statement.strip()
        if stripped.upper().startswith("CREATE"):
            assert "IF NOT EXISTS" in stripped.upper(), stripped[:120]


def test_an_upgraded_database_gains_the_civic_family_intact(tmp_path):
    """A database created before Civic existed: only the pre-Civic tables, then init_schema."""
    path = str(tmp_path / "legacy.db")
    legacy = connect(path)
    legacy.executescript(
        """
        CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            profile TEXT NOT NULL DEFAULT '',
            fetch_interval_hours INTEGER NOT NULL DEFAULT 3,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
        );
        """
    )
    legacy.commit()
    init_schema(legacy)

    present = {row["name"] for row in legacy.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'civic_%'")}
    assert set(_CIVIC_TABLES) == present
    _full_lifecycle(legacy)
    assert legacy.execute("PRAGMA foreign_key_check").fetchall() == []
    legacy.close()


def test_an_upgraded_database_enforces_the_same_civic_checks_as_a_fresh_one(tmp_path):
    fresh = connect(str(tmp_path / "fresh.db"))
    init_schema(fresh)
    legacy = connect(str(tmp_path / "legacy.db"))
    legacy.executescript("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT);")
    legacy.commit()
    init_schema(legacy)

    for table in _CIVIC_TABLES:
        fresh_sql = fresh.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,)).fetchone()["sql"]
        legacy_sql = legacy.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,)).fetchone()["sql"]
        assert fresh_sql == legacy_sql, f"{table} differs between a fresh and upgraded database"
    fresh.close()
    legacy.close()


def test_ai_revision_migration_preserves_every_existing_table_and_value(conn):
    _full_lifecycle(conn)
    conn.execute("DROP TRIGGER civic_candidates_ai_revision_guard")
    conn.execute("DROP TRIGGER civic_evidence_captures_unsealed_only")
    conn.execute("DROP INDEX idx_civic_ai_stages_one_reserved")
    conn.execute("DROP INDEX idx_civic_ai_stages_one_success")
    schema = _SCHEMA_PATH.read_text()
    for table in ("civic_qualifications", "civic_capacity_deferrals"):
        start = schema.index(f"CREATE TABLE IF NOT EXISTS {table}")
        old_sql = schema[start:schema.index(");", start) + 2]
        old_sql = old_sql.replace(
            "    ai_revision INTEGER NOT NULL DEFAULT 0 CHECK (ai_revision >= 0),\n", "")
        old_sql = old_sql.replace("    refresh_reason TEXT,\n", "")
        old_sql = old_sql.replace(
            "UNIQUE(candidate_id, ai_revision, research_attempts_used)",
            "UNIQUE(candidate_id, research_attempts_used)")
        old_sql = old_sql.replace(
            "'ai_transient_failed',\n            'ai_provenance_stale'",
            "'ai_transient_failed'")
        columns = ", ".join(
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
            if row["name"] not in {"ai_revision", "refresh_reason"})
        conn.execute(f"ALTER TABLE {table} RENAME TO before_revision_migration")
        conn.execute(old_sql)
        conn.execute(
            f"INSERT INTO {table} ({columns}) SELECT {columns} FROM before_revision_migration")
        conn.execute("DROP TABLE before_revision_migration")
    conn.execute("ALTER TABLE civic_candidates DROP COLUMN ai_revision")
    conn.execute("ALTER TABLE civic_candidates DROP COLUMN ai_refresh_reason")
    conn.execute("ALTER TABLE civic_ai_stages DROP COLUMN ai_revision")
    conn.execute(
        "CREATE UNIQUE INDEX idx_civic_ai_stages_one_reserved "
        "ON civic_ai_stages(candidate_id, stage) WHERE status = 'reserved'")
    conn.execute(
        "CREATE UNIQUE INDEX idx_civic_ai_stages_one_success "
        "ON civic_ai_stages(candidate_id, stage) WHERE status = 'succeeded'")
    conn.execute(
        "UPDATE sqlite_sequence SET seq = seq + 100 "
        "WHERE name IN ('civic_qualifications', 'civic_capacity_deferrals')")
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    tables = tuple(row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND (name NOT LIKE 'sqlite_%' OR name = 'sqlite_sequence') ORDER BY name"))
    columns = {
        table: ", ".join(row["name"] for row in conn.execute(f"PRAGMA table_info({table})"))
        for table in tables}
    before = {
        table: [tuple(row) for row in conn.execute(
            f"SELECT {columns[table]} FROM {table} ORDER BY {columns[table]}")]
        for table in tables}

    init_schema(conn)
    init_schema(conn)

    assert tuple(row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND (name NOT LIKE 'sqlite_%' OR name = 'sqlite_sequence') ORDER BY name")) == tables
    for table in tables:
        assert [
            tuple(row) for row in conn.execute(
                f"SELECT {columns[table]} FROM {table} ORDER BY {columns[table]}")
        ] == before[table], table
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    for table in ("civic_candidates", "civic_ai_stages", "civic_qualifications"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE ai_revision != 0").fetchone()[0] == 0
    assert [
        row["name"] for row in conn.execute("PRAGMA index_info(idx_civic_ai_stages_one_success)")
    ] == ["candidate_id", "stage", "ai_revision"]


def test_civic_indexes_match_between_a_fresh_and_upgraded_database(tmp_path):
    def indexes(path, bootstrap):
        c = connect(path)
        if bootstrap:
            c.executescript("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT);")
            c.commit()
        init_schema(c)
        rows = c.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name LIKE 'civic_%' ORDER BY name").fetchall()
        result = [(row["name"], row["sql"]) for row in rows]
        c.close()
        return result

    assert indexes(str(tmp_path / "a.db"), False) == indexes(str(tmp_path / "b.db"), True)


# ============================================================================
# Cascade and orphan behavior
# ============================================================================


def test_full_lifecycle_then_hard_delete_leaves_no_orphans(conn):
    watch_id = _full_lifecycle(conn)
    for table in _GENERIC_CIVIC_TABLES:
        assert conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] > 0, table

    assert hard_delete_civic_watch(conn, watch_id)

    for table in _WATCH_OWNED_TABLES:
        assert conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 0, table
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []



def test_source_outage_state_outlives_any_one_watch(conn):
    watch_id = _full_lifecycle(conn)
    conn.execute(
        "INSERT INTO civic_source_outages (source_key, unavailable_since, last_unavailable_at, "
        "observed_at) VALUES ('conservative', ?, ?, ?)",
        (T0.isoformat(), T0.isoformat(), T0.isoformat()))
    conn.commit()

    assert hard_delete_civic_watch(conn, watch_id)

    assert conn.execute("SELECT COUNT(*) AS n FROM civic_source_outages").fetchone()["n"] == 1

def test_retiring_a_watch_is_non_destructive(conn):
    watch_id = _full_lifecycle(conn)
    retire_civic_watch(conn, watch_id, T0 + timedelta(days=1))
    for table in _GENERIC_CIVIC_TABLES:
        assert conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] > 0, table


async def test_publication_tables_survive_retirement_and_cascade_on_explicit_deletion(conn):
    import json

    from beehive.civic.ai_gateway import CivicAiStage
    from beehive.db.civic_ai_stages import get_stage_receipt, output_hash_for
    from tests.civic.test_publication_novelty import claimed_batch, generated_candidate

    watch = create_civic_watch(conn, "partymap", "What changed?", T0)
    run, batch = claimed_batch(conn, watch, now=T0)
    candidate = await generated_candidate(
        conn, watch, run, batch, key="schema-fixture", now=T0, event_date=T0.date(),
        source_title="National announces a school plan",
        source_text="National announced a school plan.",
        title="National announces a school plan", summary="National announced a school plan.")
    receipt = get_stage_receipt(conn, candidate.id, CivicAiStage.GENERATE)
    invalid = receipt.output
    for content in invalid["entry"].values():
        content["title"] = "A" * 121
    conn.execute(
        "UPDATE civic_ai_stages SET output_json=?, output_hash=? WHERE id=?",
        (json.dumps(invalid), output_hash_for(invalid), receipt.id))
    conn.commit()
    sealed = seal_batch_if_claimed(conn, batch.id, run.id, run.claim_token, T0)
    frozen = get_frozen_publication(conn, sealed.id)
    rejections = list_publication_rejections(conn, sealed.id)
    assert sealed.candidate_count == 0
    assert frozen is not None
    assert len(rejections) == 1
    assert rejections[0].reason == "publication_schema_invalid"
    for table in _PUBLICATION_TABLES:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1

    retire_civic_watch(conn, watch.id, T0 + timedelta(days=1))
    assert get_frozen_publication(conn, sealed.id) == frozen
    assert list_publication_rejections(conn, sealed.id) == rejections
    assert hard_delete_civic_watch(conn, watch.id)
    for table in _WATCH_OWNED_TABLES:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_deleting_a_run_never_deletes_its_watch_or_vocabulary(conn):
    watch_id = _full_lifecycle(conn)
    conn.execute("DELETE FROM civic_runs WHERE watch_id = ?", (watch_id,))
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM civic_watches").fetchone()["n"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM civic_vocabulary_versions").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM civic_batches").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM civic_candidates").fetchone()["n"] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_a_pinned_vocabulary_version_cannot_be_deleted_out_from_under_a_run(conn):
    _full_lifecycle(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM civic_vocabulary_versions")
    conn.rollback()


# ============================================================================
# Constraints bite even against raw SQL
# ============================================================================


def test_one_run_per_slot_is_enforced_by_the_schema(conn):
    watch_id = create_civic_watch(conn, "W", "Q", T0).id
    enqueue_civic_run(conn, watch_id, T0, T0)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO civic_runs (watch_id, slot_at, status, requested_at) "
            "VALUES (?, ?, 'failed', ?)",
            (watch_id, T0.isoformat(), T0.isoformat()))


def test_one_active_run_per_watch_is_enforced_by_the_schema(conn):
    watch_id = create_civic_watch(conn, "W", "Q", T0).id
    enqueue_civic_run(conn, watch_id, T0, T0)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO civic_runs (watch_id, slot_at, status, requested_at) "
            "VALUES (?, ?, 'pending', ?)",
            (watch_id, (T0 + timedelta(days=1)).isoformat(), T0.isoformat()))


def test_a_processing_run_row_must_carry_its_whole_claim(conn):
    watch_id = create_civic_watch(conn, "W", "Q", T0).id
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO civic_runs (watch_id, slot_at, status, phase, requested_at) "
            "VALUES (?, ?, 'processing', 'gathering', ?)",
            (watch_id, T0.isoformat(), T0.isoformat()))


def test_a_terminal_run_row_must_carry_completed_at(conn):
    watch_id = create_civic_watch(conn, "W", "Q", T0).id
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO civic_runs (watch_id, slot_at, status, requested_at) "
            "VALUES (?, ?, 'completed', ?)",
            (watch_id, T0.isoformat(), T0.isoformat()))


def test_only_one_published_vocabulary_version_per_watch(conn):
    watch_id = create_civic_watch(conn, "W", "Q", T0).id
    version = create_vocabulary_version(conn, watch_id, T0)
    add_vocabulary_term(
        conn, version.id, "Green Party", CivicTermCategory.PARTY, CivicTermRole.REQUIRED, T0)
    publish_vocabulary_version(conn, version.id, T0)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO civic_vocabulary_versions (watch_id, version, status, content_hash, "
            "created_at, published_at) VALUES (?, 99, 'published', 'h', ?, ?)",
            (watch_id, T0.isoformat(), T0.isoformat()))


def test_a_draft_vocabulary_row_may_not_carry_a_hash(conn):
    watch_id = create_civic_watch(conn, "W", "Q", T0).id
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO civic_vocabulary_versions (watch_id, version, status, content_hash, "
            "created_at) VALUES (?, 1, 'draft', 'sneaky-hash', ?)",
            (watch_id, T0.isoformat()))


def test_a_sealed_batch_row_must_carry_its_hash(conn):
    watch_id = create_civic_watch(conn, "W", "Q", T0).id
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO civic_batches (run_id, watch_id, status, idempotency_key, created_at, "
            "sealed_at) VALUES (?, ?, 'sealed', 'k', ?, ?)",
            (run.id, watch_id, T0.isoformat(), T0.isoformat()))


def test_a_rejected_candidate_row_must_carry_a_rejection_code(conn):
    watch_id = _full_lifecycle(conn)
    run_id = conn.execute(
        "SELECT id FROM civic_runs WHERE watch_id = ?", (watch_id,)).fetchone()["id"]
    batch_id = conn.execute(
        "SELECT id FROM civic_batches WHERE run_id = ?", (run_id,)).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO civic_candidates (watch_id, run_id, batch_id, dedupe_key, title, "
            "statement, status, created_at, settled_at) "
            "VALUES (?, ?, ?, 'dk-x', 'T', 'S', 'rejected', ?, ?)",
            (watch_id, run_id, batch_id, T0.isoformat(), T0.isoformat()))


def test_a_candidate_row_may_not_supersede_itself(conn):
    watch_id = _full_lifecycle(conn)
    candidate_id = conn.execute(
        "SELECT id FROM civic_candidates WHERE watch_id = ?", (watch_id,)).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE civic_candidates SET status = 'superseded', "
            "superseded_by_candidate_id = id WHERE id = ?", (candidate_id,))


def test_only_a_verified_candidate_row_may_carry_a_seal_stamp(conn):
    watch_id = create_civic_watch(conn, "W", "Q", T0).id
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, T0)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO civic_candidates (watch_id, run_id, batch_id, dedupe_key, title, "
            "statement, status, created_at, sealed_into_batch_at) "
            "VALUES (?, ?, ?, 'dk-x', 'T', 'S', 'drafted', ?, ?)",
            (watch_id, run.id, batch.id, T0.isoformat(), T0.isoformat()))


def test_a_rejected_candidate_row_may_not_carry_a_seal_stamp(conn):
    watch_id = create_civic_watch(conn, "W", "Q", T0).id
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, T0)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO civic_candidates (watch_id, run_id, batch_id, dedupe_key, title, "
            "statement, status, created_at, settled_at, rejection_code, sealed_into_batch_at) "
            "VALUES (?, ?, ?, 'dk-x', 'T', 'S', 'rejected', ?, ?, 'off_topic', ?)",
            (watch_id, run.id, batch.id, T0.isoformat(), T0.isoformat(), T0.isoformat()))


def test_a_civic_run_may_not_pin_another_watchs_vocabulary_through_the_repository(conn):
    """The repository gate, not a schema constraint: SQLite cannot express a cross-table
    "same watch_id" rule, so db/civic_runs.py enforces it under BEGIN IMMEDIATE instead."""
    watch_id = create_civic_watch(conn, "W", "Q", T0).id
    other_watch = create_civic_watch(conn, "Other", "Q", T0).id
    other_version = create_vocabulary_version(conn, other_watch, T0)
    add_vocabulary_term(
        conn, other_version.id, "Green Party", CivicTermCategory.PARTY, CivicTermRole.REQUIRED,
        T0)
    publish_vocabulary_version(conn, other_version.id, T0)

    with pytest.raises(ValueError, match="belongs to Civic Watch"):
        enqueue_civic_run(conn, watch_id, T0, T0, vocabulary_version_id=other_version.id)
    assert conn.execute("SELECT COUNT(*) AS n FROM civic_runs").fetchone()["n"] == 0


def test_evidence_tiers_and_stances_are_constrained(conn):
    watch_id = _full_lifecycle(conn)
    candidate_id = conn.execute(
        "SELECT id FROM civic_candidates WHERE watch_id = ?", (watch_id,)).fetchone()["id"]
    for column, bad in (("tier", "blog"), ("stance", "maybe")):
        values = {"tier": "official", "stance": "supporting"}
        values[column] = bad
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO civic_candidate_evidence (candidate_id, source_ref, external_key, "
                "url, title, tier, stance, captured_at) "
                "VALUES (?, 'src', 'x', 'u', 't', ?, ?, ?)",
                (candidate_id, values["tier"], values["stance"], T0.isoformat()))


def test_a_notification_row_may_not_hold_a_claim_outside_sending(conn):
    watch_id = create_civic_watch(conn, "W", "Q", T0).id
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO civic_notifications (watch_id, kind, dedupe_key, subject, status, "
            "claim_token, lease_expires_at, queued_at) "
            "VALUES (?, 'k', 'd', 's', 'pending', 'tok', ?, ?)",
            (watch_id, T0.isoformat(), T0.isoformat()))


def test_a_receipt_channel_is_constrained(conn):
    watch_id = _full_lifecycle(conn)
    batch_id = conn.execute(
        "SELECT id FROM civic_batches WHERE watch_id = ?", (watch_id,)).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO civic_delivery_receipts (batch_id, idempotency_key, content_hash, "
            "channel, target, delivered_at) VALUES (?, 'other-key', 'h', 'carrier-pigeon', "
            "'somewhere', ?)",
            (batch_id, T0.isoformat()))
