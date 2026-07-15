import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.items import get_item, mark_read
from beehive.db.sources import create_source
from beehive.db.summary_rewrites import (
    apply_summary_rewrite,
    count_for_run,
    delete_entry,
    list_for_run,
    revert_summary_rewrite_entry,
    was_migrated,
)

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


def _make_item(conn, external_id="t1", score=50.0, summary="old summary"):
    channel_id = create_channel(conn, f"Channel {external_id}", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url, ai_score, ai_summary, "
        "ai_rationale) VALUES (?, ?, 'Title', 'https://x', ?, ?, 'r')",
        (source_id, external_id, score, summary))
    conn.commit()
    return conn.execute(
        "SELECT id FROM items WHERE external_id = ?", (external_id,)).fetchone()[0]


def test_was_migrated_false_before_any_apply(conn):
    item_id = _make_item(conn)
    assert was_migrated(conn, "run-1", item_id) is False


# ============================================================================
# apply_summary_rewrite: the single atomic seam (item UPDATE + log INSERT, one transaction)
# ============================================================================

def test_apply_summary_rewrite_writes_the_summary_and_logs_it_together(conn):
    item_id = _make_item(conn, score=77.0, summary="old summary")

    previous = apply_summary_rewrite(conn, "run-1", item_id, "new summary",
                                      high_water_item_id=item_id, now=T0)

    assert previous == "old summary"
    row = get_item(conn, item_id)
    assert row["ai_summary"] == "new summary"
    assert row["ai_score"] == 77.0  # score untouched
    assert row["ai_rationale"] == "r"  # rationale untouched
    assert row["is_read"] == 0  # read state untouched

    entries = list_for_run(conn, "run-1")
    assert len(entries) == 1
    assert entries[0].item_id == item_id
    assert entries[0].previous_summary == "old summary"
    assert entries[0].replacement_summary == "new summary"
    assert entries[0].migrated_at == T0.isoformat()
    assert was_migrated(conn, "run-1", item_id) is True


def test_apply_summary_rewrite_returns_none_and_writes_nothing_when_item_is_read(conn):
    item_id = _make_item(conn, summary="old summary")
    mark_read(conn, item_id)

    previous = apply_summary_rewrite(conn, "run-1", item_id, "new summary",
                                      high_water_item_id=item_id, now=T0)

    assert previous is None
    assert get_item(conn, item_id)["ai_summary"] == "old summary"
    assert list_for_run(conn, "run-1") == []


def test_apply_summary_rewrite_returns_none_and_writes_nothing_above_high_water_mark(conn):
    item_id = _make_item(conn, summary="old summary")

    previous = apply_summary_rewrite(conn, "run-1", item_id, "new summary",
                                      high_water_item_id=item_id - 1, now=T0)

    assert previous is None
    assert get_item(conn, item_id)["ai_summary"] == "old summary"
    assert list_for_run(conn, "run-1") == []


# ============================================================================
# Regression: item UPDATE and log INSERT must be one transaction -- a failure in the log
# INSERT must never leave a live rewritten summary with no audit/rollback row.
# ============================================================================

class _FailingExecuteConnection:
    """Thin delegating proxy around a real sqlite3.Connection that fails one specific
    statement. sqlite3.Connection is a C extension type whose `execute` attribute cannot be
    monkeypatched directly (it is read-only on the instance), so this wraps the real
    connection instead: `execute` is intercepted here, and everything else (commit, rollback,
    row_factory, ...) passes straight through to the real connection via __getattr__, so
    apply_summary_rewrite's own conn.commit()/conn.rollback() calls act on the SAME real
    connection/transaction as its conn.execute() calls did."""

    def __init__(self, real_conn: sqlite3.Connection, should_fail):
        self._real_conn = real_conn
        self._should_fail = should_fail

    def execute(self, sql, parameters=()):
        if self._should_fail(sql):
            raise sqlite3.OperationalError(f"simulated failure for: {sql}")
        return self._real_conn.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._real_conn, name)


def test_apply_summary_rewrite_rolls_back_the_item_update_if_the_log_insert_fails(conn):
    """Forces the summary_rewrite_log INSERT to fail (simulating a crash or any exception
    between the two writes) and proves the ai_summary UPDATE that ran just before it was
    rolled back too, rather than left committed with no matching log row -- the bug this
    single-transaction seam exists to close."""
    item_id = _make_item(conn, summary="old summary")
    failing_conn = _FailingExecuteConnection(
        conn, should_fail=lambda sql: "INSERT INTO summary_rewrite_log" in sql)

    with pytest.raises(sqlite3.OperationalError, match="simulated"):
        apply_summary_rewrite(failing_conn, "run-1", item_id, "new summary",
                               high_water_item_id=item_id, now=T0)

    # The item's ai_summary UPDATE ran (and would have succeeded) immediately before the
    # failing INSERT -- proving it did NOT survive the rollback is the whole point here.
    assert get_item(conn, item_id)["ai_summary"] == "old summary"
    assert list_for_run(conn, "run-1") == []
    assert was_migrated(conn, "run-1", item_id) is False

    # The connection must be left usable (transaction cleanly closed, not left dangling) --
    # a subsequent call must succeed normally.
    previous = apply_summary_rewrite(conn, "run-1", item_id, "new summary",
                                      high_water_item_id=item_id, now=T0)
    assert previous == "old summary"
    assert get_item(conn, item_id)["ai_summary"] == "new summary"


def test_apply_summary_rewrite_rolls_back_if_the_item_update_itself_fails(conn):
    """Same guarantee from the other direction: if the ai_summary UPDATE itself fails, no log
    row must ever be inserted for it."""
    item_id = _make_item(conn, summary="old summary")
    failing_conn = _FailingExecuteConnection(
        conn, should_fail=lambda sql: sql.startswith("UPDATE items SET ai_summary"))

    with pytest.raises(sqlite3.OperationalError, match="simulated"):
        apply_summary_rewrite(failing_conn, "run-1", item_id, "new summary",
                               high_water_item_id=item_id, now=T0)

    assert get_item(conn, item_id)["ai_summary"] == "old summary"
    assert list_for_run(conn, "run-1") == []


# ============================================================================
# Idempotent / concurrent-duplicate-call no-op behavior
# ============================================================================

def test_apply_summary_rewrite_is_idempotent_for_a_duplicate_call_under_the_same_run_id(conn):
    """Simulates two invocations racing (or a caller retrying) for the same (run_id, item_id):
    the second call must be a clean no-op -- it must not overwrite the first call's summary,
    must not touch the original previous_summary the log holds, and must not create a second
    log row."""
    item_id = _make_item(conn, summary="old summary")

    first = apply_summary_rewrite(conn, "run-1", item_id, "first new summary",
                                   high_water_item_id=item_id, now=T0)
    second = apply_summary_rewrite(conn, "run-1", item_id, "second new summary",
                                    high_water_item_id=item_id, now=T0)

    assert first == "old summary"
    assert second is None
    assert get_item(conn, item_id)["ai_summary"] == "first new summary"
    assert count_for_run(conn, "run-1") == 1
    entries = list_for_run(conn, "run-1")
    assert entries[0].previous_summary == "old summary"
    assert entries[0].replacement_summary == "first new summary"


def test_apply_summary_rewrite_different_run_id_is_independent(conn):
    item_id = _make_item(conn, summary="old summary")
    apply_summary_rewrite(conn, "run-1", item_id, "new summary",
                           high_water_item_id=item_id, now=T0)

    previous = apply_summary_rewrite(conn, "run-2", item_id, "another new summary",
                                      high_water_item_id=item_id, now=T0)

    assert previous == "new summary"  # run-2 sees run-1's write as its own "previous" value
    assert get_item(conn, item_id)["ai_summary"] == "another new summary"
    assert count_for_run(conn, "run-1") == 1
    assert count_for_run(conn, "run-2") == 1


def test_apply_summary_rewrite_serializes_concurrent_calls_for_the_same_run(tmp_path):
    db_path = str(tmp_path / "concurrent.db")
    setup = connect(db_path)
    init_schema(setup)
    item_id = _make_item(setup, summary="old summary")
    setup.close()

    barrier = threading.Barrier(3)
    results = []
    errors = []

    def apply(replacement):
        connection = connect(db_path)
        try:
            barrier.wait()
            results.append(apply_summary_rewrite(
                connection, "run-1", item_id, replacement,
                high_water_item_id=item_id, now=T0))
        except BaseException as exc:
            errors.append(exc)
        finally:
            connection.close()

    first = threading.Thread(target=apply, args=("first replacement",))
    second = threading.Thread(target=apply, args=("second replacement",))
    first.start()
    second.start()
    barrier.wait()
    first.join()
    second.join()

    assert errors == []
    assert sorted(result is None for result in results) == [False, True]

    check = connect(db_path)
    entry = list_for_run(check, "run-1")[0]
    assert count_for_run(check, "run-1") == 1
    assert entry.previous_summary == "old summary"
    assert get_item(check, item_id)["ai_summary"] == entry.replacement_summary
    check.close()


# ============================================================================
# list_for_run / count_for_run / delete_entry
# ============================================================================

def test_list_for_run_returns_only_entries_for_that_run_in_insertion_order(conn):
    item_a = _make_item(conn, "t1")
    item_b = _make_item(conn, "t2")
    item_c = _make_item(conn, "t3")
    apply_summary_rewrite(conn, "run-1", item_b, "new-b", high_water_item_id=item_c, now=T0)
    apply_summary_rewrite(conn, "run-1", item_a, "new-a", high_water_item_id=item_c, now=T0)
    apply_summary_rewrite(conn, "run-2", item_c, "new-c", high_water_item_id=item_c, now=T0)

    entries = list_for_run(conn, "run-1")

    assert [e.item_id for e in entries] == [item_b, item_a]


def test_delete_entry_removes_the_row(conn):
    item_id = _make_item(conn)
    apply_summary_rewrite(conn, "run-1", item_id, "new summary",
                           high_water_item_id=item_id, now=T0)
    entry = list_for_run(conn, "run-1")[0]

    delete_entry(conn, entry.id)

    assert list_for_run(conn, "run-1") == []
    assert was_migrated(conn, "run-1", item_id) is False


def test_revert_summary_rewrite_entry_restores_and_deletes_atomically(conn):
    item_id = _make_item(conn)
    apply_summary_rewrite(conn, "run-1", item_id, "new summary",
                          high_water_item_id=item_id, now=T0)
    entry = list_for_run(conn, "run-1")[0]

    assert revert_summary_rewrite_entry(conn, entry) is True

    assert get_item(conn, item_id)["ai_summary"] == "old summary"
    assert list_for_run(conn, "run-1") == []


def test_revert_summary_rewrite_entry_retains_log_when_summary_changed(conn):
    item_id = _make_item(conn)
    apply_summary_rewrite(conn, "run-1", item_id, "new summary",
                          high_water_item_id=item_id, now=T0)
    entry = list_for_run(conn, "run-1")[0]
    conn.execute("UPDATE items SET ai_summary = 'later change' WHERE id = ?", (item_id,))
    conn.commit()

    assert revert_summary_rewrite_entry(conn, entry) is False

    assert get_item(conn, item_id)["ai_summary"] == "later change"
    assert list_for_run(conn, "run-1") == [entry]


def test_revert_summary_rewrite_entry_rolls_back_restore_if_log_delete_fails(conn):
    item_id = _make_item(conn)
    apply_summary_rewrite(conn, "run-1", item_id, "new summary",
                          high_water_item_id=item_id, now=T0)
    entry = list_for_run(conn, "run-1")[0]
    failing_conn = _FailingExecuteConnection(
        conn, should_fail=lambda sql: sql.startswith("DELETE FROM summary_rewrite_log"))

    with pytest.raises(sqlite3.OperationalError, match="simulated"):
        revert_summary_rewrite_entry(failing_conn, entry)

    assert get_item(conn, item_id)["ai_summary"] == "new summary"
    assert list_for_run(conn, "run-1") == [entry]


def test_count_for_run_counts_only_matching_run_id(conn):
    item_a = _make_item(conn, "t1")
    item_b = _make_item(conn, "t2")
    apply_summary_rewrite(conn, "run-1", item_a, "new-a", high_water_item_id=item_b, now=T0)
    apply_summary_rewrite(conn, "run-1", item_b, "new-b", high_water_item_id=item_b, now=T0)
    apply_summary_rewrite(conn, "run-2", item_a, "new-a2", high_water_item_id=item_b, now=T0)

    assert count_for_run(conn, "run-1") == 2
    assert count_for_run(conn, "run-2") == 1
