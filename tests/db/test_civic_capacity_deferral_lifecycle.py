# tests/db/test_civic_capacity_deferral_lifecycle.py
"""Deleting the Runs, Batches, Candidates and Watches a capacity deferral points at.

A deferral row names four things, and they do not all mean the same thing to it:

  * its **Candidate** is what the row is about, so the row cascades away with it;
  * its **source** (run, batch) is where the Candidate was held back, which is the row's whole
    reason to exist, so that cascades too;
  * its **destination** (run, batch) is where the Candidate went afterwards, which is evidence
    rather than structure -- the hop happened whether or not the Batch still exists, so deleting
    the destination nulls the pointer and leaves `adopted_at` alone.

The destination is one composite foreign key rather than two independent ones, and that is what
this suite pins. With two separate keys, deleting just the destination Batch would null
to_batch_id and leave to_run_id naming a Batch that is gone -- a state the row's own "either both
or neither" CHECK forbids, so the delete would fail on a constraint instead of doing the obvious
thing. A composite ON DELETE SET NULL nulls every column of the key together, so the destination
is always a whole answer or no answer at all.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.civic_batches import (adopt_capacity_deferrals, count_pending_capacity_deferrals,
                                      get_or_create_batch_if_claimed,
                                      list_candidate_capacity_deferrals,
                                      list_pending_capacity_deferrals, seal_batch_if_claimed)
from beehive.db.civic_candidates import (add_candidate_evidence, create_candidate,
                                         mark_candidate_verified, set_candidate_status)
from beehive.db.civic_runs import claim_civic_run, complete_civic_run, enqueue_civic_run
from beehive.db.civic_verifications import record_verification
from beehive.db.civic_watches import create_civic_watch, hard_delete_civic_watch
from beehive.db.connection import _SCHEMA_PATH, connect, init_schema
from beehive.domain.civic import (CivicCandidateStatus, CivicEvidenceStance, CivicEvidenceTier,
                                  CivicRunStatus, CivicVerificationVerdict)

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


def _slot(conn, watch_id, at):
    run = enqueue_civic_run(conn, watch_id, at, at)
    lease = claim_civic_run(conn, run.id, at, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, at)
    return run.id, lease.run.claim_token, batch


def _verified(conn, batch_id, key, at=T0):
    candidate = create_candidate(conn, batch_id, key, f"Title {key}", f"Body {key}", at)
    add_candidate_evidence(
        conn, candidate.id, "official_feed", f"e-{key}", f"https://example.test/{key}",
        "Release", CivicEvidenceTier.OFFICIAL, CivicEvidenceStance.SUPPORTING, at,
        publisher="Beehive")
    set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, at)
    record_verification(conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", at)
    return mark_candidate_verified(conn, candidate.id, at)


@pytest.fixture
def open_deferral(conn):
    """One Watch holding carryovers nobody has adopted yet: open rows, no destination.

    Thirteen Candidates against the cap of six leaves seven waiting, which is what lets the next
    slot fill itself entirely from the queue and push the last one on again."""
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    run_id, token, batch = _slot(conn, watch_id, T0)
    candidates = [_verified(conn, batch.id, f"dk-{index:02d}") for index in range(13)]
    sealed = seal_batch_if_claimed(conn, batch.id, run_id, token, T0)
    complete_civic_run(conn, run_id, token, CivicRunStatus.COMPLETED, T0)
    assert count_pending_capacity_deferrals(conn, watch_id) == 7
    return type("Ctx", (), {
        "watch_id": watch_id, "run_id": run_id, "batch_id": sealed.id,
        "candidate": candidates[12], "candidates": candidates})()


@pytest.fixture
def closed_deferral(conn, open_deferral):
    """A carryover that has moved on twice, so its first hop's destination is a Batch it no
    longer lives in.

    That shape is the only one in which deleting the destination is observable at all: while the
    Candidate is still *in* the destination Batch, deleting it deletes the Candidate and takes
    the whole row with it. Here the second slot fills itself from the queue and pushes the last
    Candidate on to a third, leaving row one closed against a Batch that is now merely history."""
    second_run, second_token, second_batch = _slot(conn, open_deferral.watch_id, T0 + DAY)
    adopt_capacity_deferrals(conn, second_batch.id, second_run, second_token, T0 + DAY)
    seal_batch_if_claimed(conn, second_batch.id, second_run, second_token, T0 + DAY)
    complete_civic_run(conn, second_run, second_token, CivicRunStatus.COMPLETED, T0 + DAY)
    assert count_pending_capacity_deferrals(conn, open_deferral.watch_id) == 1

    third_run, third_token, third_batch = _slot(conn, open_deferral.watch_id, T0 + 2 * DAY)
    adopt_capacity_deferrals(conn, third_batch.id, third_run, third_token, T0 + 2 * DAY)

    history = list_candidate_capacity_deferrals(conn, open_deferral.candidate.id)
    assert len(history) == 2
    assert history[0].to_batch_id == second_batch.id
    assert history[1].to_batch_id == third_batch.id
    return type("Ctx", (), {
        "watch_id": open_deferral.watch_id, "candidate": open_deferral.candidate,
        "source_run_id": open_deferral.run_id, "source_batch_id": open_deferral.batch_id,
        "dest_run_id": second_run, "dest_batch_id": second_batch.id})()


def _history(conn, candidate_id):
    return list_candidate_capacity_deferrals(conn, candidate_id)


# ============================================================================
# The constraint shape
# ============================================================================


def test_the_destination_is_one_composite_foreign_key(conn):
    sql = " ".join(conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("civic_capacity_deferrals",)).fetchone()[0].split())
    assert ("FOREIGN KEY (to_run_id, to_batch_id) REFERENCES civic_batches(run_id, id) "
            "ON DELETE SET NULL") in sql
    assert ("FOREIGN KEY (from_run_id, from_batch_id) REFERENCES civic_batches(run_id, id) "
            "ON DELETE CASCADE") in sql
    # Two independent SET NULL references would be the bug this replaced.
    assert "to_run_id INTEGER REFERENCES" not in sql
    assert "to_batch_id INTEGER REFERENCES" not in sql


@pytest.mark.parametrize("to_run,to_batch,adopted", [
    (1, None, "2026-08-02T06:00:00+00:00"),   # half a destination
    (None, 1, "2026-08-02T06:00:00+00:00"),   # the other half
    (1, 1, None),                             # a destination on an open row
])
def test_the_invariants_still_reject_a_half_answer(conn, open_deferral, to_run, to_batch,
                                                   adopted):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE civic_capacity_deferrals SET to_run_id = ?, to_batch_id = ?, adopted_at = ? "
            "WHERE candidate_id = ?",
            (to_run, to_batch, adopted, open_deferral.candidate.id))


def test_an_adopted_timestamp_may_outlive_its_destination(conn, closed_deferral):
    conn.execute(
        "UPDATE civic_capacity_deferrals SET to_run_id = NULL, to_batch_id = NULL "
        "WHERE candidate_id = ? AND to_batch_id = ?",
        (closed_deferral.candidate.id, closed_deferral.dest_batch_id))
    conn.commit()
    first = _history(conn, closed_deferral.candidate.id)[0]
    assert first.adopted_at is not None
    assert first.to_run_id is None and first.to_batch_id is None


def test_an_unknown_reason_is_rejected(conn, open_deferral):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE civic_capacity_deferrals SET reason = 'because' WHERE candidate_id = ?",
            (open_deferral.candidate.id,))


# ============================================================================
# Deleting the destination
# ============================================================================


def test_deleting_the_destination_batch_nulls_the_whole_destination(conn, closed_deferral):
    conn.execute("DELETE FROM civic_batches WHERE id = ?", (closed_deferral.dest_batch_id,))
    conn.commit()

    first = _history(conn, closed_deferral.candidate.id)[0]
    assert first.to_run_id is None and first.to_batch_id is None
    assert first.adopted_at is not None, "the hop still happened"
    assert first.from_batch_id == closed_deferral.source_batch_id
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_deleting_the_destination_run_nulls_the_whole_destination(conn, closed_deferral):
    conn.execute("DELETE FROM civic_runs WHERE id = ?", (closed_deferral.dest_run_id,))
    conn.commit()

    first = _history(conn, closed_deferral.candidate.id)[0]
    assert first.to_run_id is None and first.to_batch_id is None
    assert first.adopted_at is not None
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_deleting_a_destination_the_candidate_still_lives_in_takes_the_row(conn, open_deferral):
    """When the Candidate is still in the destination Batch, deleting it deletes the Candidate
    too -- so the row goes with its subject rather than surviving as a decision about nothing."""
    second_run, second_token, second_batch = _slot(conn, open_deferral.watch_id, T0 + DAY)
    adopt_capacity_deferrals(conn, second_batch.id, second_run, second_token, T0 + DAY)
    conn.execute("DELETE FROM civic_batches WHERE id = ?", (second_batch.id,))
    conn.commit()

    assert _history(conn, open_deferral.candidate.id) == []
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM civic_candidates WHERE id = ?",
        (open_deferral.candidate.id,)).fetchone()["n"] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# ============================================================================
# Deleting the source, the Candidate and the Watch
# ============================================================================


def test_deleting_the_source_batch_takes_the_row(conn, open_deferral):
    conn.execute("DELETE FROM civic_batches WHERE id = ?", (open_deferral.batch_id,))
    conn.commit()
    assert _history(conn, open_deferral.candidate.id) == []
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_deleting_the_source_run_takes_the_row(conn, closed_deferral):
    conn.execute("DELETE FROM civic_runs WHERE id = ?", (closed_deferral.source_run_id,))
    conn.commit()
    remaining = _history(conn, closed_deferral.candidate.id)
    assert all(entry.from_batch_id != closed_deferral.source_batch_id for entry in remaining)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_deleting_the_candidate_takes_every_row_about_it(conn, closed_deferral):
    conn.execute(
        "DELETE FROM civic_candidates WHERE id = ?", (closed_deferral.candidate.id,))
    conn.commit()
    assert _history(conn, closed_deferral.candidate.id) == []
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("fixture_name", ["open_deferral", "closed_deferral"])
def test_hard_deleting_the_watch_succeeds_whatever_state_the_deferral_is_in(
        conn, request, fixture_name):
    context = request.getfixturevalue(fixture_name)
    assert hard_delete_civic_watch(conn, context.watch_id) is True
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM civic_capacity_deferrals").fetchone()["n"] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_the_pending_reads_follow_the_deletions(conn, open_deferral):
    assert count_pending_capacity_deferrals(conn, open_deferral.watch_id) == 7
    assert len(list_pending_capacity_deferrals(conn, open_deferral.watch_id)) == 7
    conn.execute(
        "DELETE FROM civic_candidates WHERE id = ?", (open_deferral.candidate.id,))
    conn.commit()
    assert count_pending_capacity_deferrals(conn, open_deferral.watch_id) == 6
    assert open_deferral.candidate.id not in {
        entry.candidate_id for entry in list_pending_capacity_deferrals(
            conn, open_deferral.watch_id)}

    hard_delete_civic_watch(conn, open_deferral.watch_id)
    assert count_pending_capacity_deferrals(conn, open_deferral.watch_id) == 0
    assert list_pending_capacity_deferrals(conn, open_deferral.watch_id) == []


# ============================================================================
# Fresh and upgraded schemas agree
# ============================================================================


def _legacy_definition() -> str:
    """The first cut of the table: two independent SET NULL references and the CHECK that made
    them unsatisfiable."""
    return """
CREATE TABLE civic_capacity_deferrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id INTEGER NOT NULL REFERENCES civic_watches(id) ON DELETE CASCADE,
    candidate_id INTEGER NOT NULL REFERENCES civic_candidates(id) ON DELETE CASCADE,
    from_run_id INTEGER NOT NULL REFERENCES civic_runs(id) ON DELETE CASCADE,
    from_batch_id INTEGER NOT NULL REFERENCES civic_batches(id) ON DELETE CASCADE,
    to_run_id INTEGER REFERENCES civic_runs(id) ON DELETE SET NULL,
    to_batch_id INTEGER REFERENCES civic_batches(id) ON DELETE SET NULL,
    published_cap INTEGER NOT NULL CHECK (published_cap > 0),
    run_mode TEXT NOT NULL CHECK (run_mode IN ('normal', 'election')),
    reason TEXT NOT NULL DEFAULT 'batch_cap_reached',
    deferred_at TEXT NOT NULL,
    adopted_at TEXT,
    UNIQUE(candidate_id, from_batch_id),
    CHECK (from_batch_id != to_batch_id),
    CHECK (
        (adopted_at IS NULL AND to_run_id IS NULL AND to_batch_id IS NULL)
        OR (adopted_at IS NOT NULL AND to_run_id IS NOT NULL AND to_batch_id IS NOT NULL)
    )
)"""


def _legacy_database(path) -> sqlite3.Connection:
    """A database exactly as an early adopter's would be: the current schema everywhere except
    this one table, which still carries its first definition."""
    conn = connect(str(path))
    conn.executescript(_SCHEMA_PATH.read_text())
    conn.executescript(
        "PRAGMA foreign_keys = OFF;\n"
        "DROP TABLE civic_capacity_deferrals;\n"
        + _legacy_definition() + ";\n"
        "CREATE INDEX idx_civic_capacity_deferrals_candidate "
        "ON civic_capacity_deferrals(candidate_id, id);\n"
        "CREATE INDEX idx_civic_capacity_deferrals_open "
        "ON civic_capacity_deferrals(watch_id, candidate_id) WHERE adopted_at IS NULL;\n"
        "PRAGMA foreign_keys = ON;")
    conn.commit()
    return conn


def test_an_upgraded_database_gains_the_composite_destination_key(tmp_path):
    legacy = _legacy_database(tmp_path / "legacy.db")
    stored = " ".join(legacy.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'civic_capacity_deferrals'"
    ).fetchone()[0].split())
    assert "FOREIGN KEY (to_run_id, to_batch_id)" not in stored

    init_schema(legacy)
    upgraded = " ".join(legacy.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'civic_capacity_deferrals'"
    ).fetchone()[0].split())
    fresh_conn = connect(str(tmp_path / "fresh.db"))
    init_schema(fresh_conn)
    fresh = " ".join(fresh_conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'civic_capacity_deferrals'"
    ).fetchone()[0].split())
    assert upgraded == fresh


def test_the_upgrade_carries_existing_deferral_rows_across(tmp_path):
    legacy = _legacy_database(tmp_path / "legacy.db")
    watch_id = create_civic_watch(legacy, "NZ election", "Q", T0).id
    run_id, token, batch = _slot(legacy, watch_id, T0)
    candidate = _verified(legacy, batch.id, "dk-1")
    legacy.execute(
        "INSERT INTO civic_capacity_deferrals (watch_id, candidate_id, from_run_id, "
        "from_batch_id, published_cap, run_mode, reason, deferred_at) "
        "VALUES (?, ?, ?, ?, 6, 'normal', 'batch_cap_reached', ?)",
        (watch_id, candidate.id, run_id, batch.id, T0.isoformat()))
    legacy.commit()

    init_schema(legacy)
    carried = list_candidate_capacity_deferrals(legacy, candidate.id)
    assert len(carried) == 1
    assert carried[0].from_batch_id == batch.id
    assert carried[0].is_open
    assert legacy.execute("PRAGMA foreign_key_check").fetchall() == []


def test_the_upgraded_database_can_delete_a_destination(tmp_path):
    """The whole point: on the legacy definition this delete failed on a CHECK."""
    legacy = _legacy_database(tmp_path / "legacy.db")
    init_schema(legacy)

    watch_id = create_civic_watch(legacy, "NZ election", "Q", T0).id
    run_id, token, batch = _slot(legacy, watch_id, T0)
    candidates = [_verified(legacy, batch.id, f"dk-{index:02d}") for index in range(13)]
    seal_batch_if_claimed(legacy, batch.id, run_id, token, T0)
    complete_civic_run(legacy, run_id, token, CivicRunStatus.COMPLETED, T0)

    second_run, second_token, second_batch = _slot(legacy, watch_id, T0 + DAY)
    adopt_capacity_deferrals(legacy, second_batch.id, second_run, second_token, T0 + DAY)
    seal_batch_if_claimed(legacy, second_batch.id, second_run, second_token, T0 + DAY)
    complete_civic_run(legacy, second_run, second_token, CivicRunStatus.COMPLETED, T0 + DAY)

    third_run, third_token, third_batch = _slot(legacy, watch_id, T0 + 2 * DAY)
    adopt_capacity_deferrals(legacy, third_batch.id, third_run, third_token, T0 + 2 * DAY)

    legacy.execute("DELETE FROM civic_batches WHERE id = ?", (second_batch.id,))
    legacy.commit()
    first = list_candidate_capacity_deferrals(legacy, candidates[12].id)[0]
    assert first.to_run_id is None and first.to_batch_id is None
    assert first.adopted_at is not None


def test_the_migration_leaves_a_current_table_untouched(conn):
    before = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'civic_capacity_deferrals'").fetchone()[0]
    rowid_before = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'civic_capacity_deferrals'").fetchone()
    init_schema(conn)
    init_schema(conn)
    after = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'civic_capacity_deferrals'").fetchone()[0]
    assert after == before
    assert conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'civic_capacity_deferrals'"
    ).fetchone() == rowid_before
