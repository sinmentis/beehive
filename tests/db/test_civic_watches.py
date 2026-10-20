# tests/db/test_civic_watches.py
"""Civic Watch persistence: identity, lifecycle transitions, and the schedulability rule the
Civic Run queue depends on."""
from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.civic_watches import (create_civic_watch, get_civic_watch,
                                       get_civic_watch_by_subject, hard_delete_civic_watch,
                                       list_civic_watches, list_schedulable_civic_watches,
                                       pause_civic_watch, resume_civic_watch,
                                       retire_civic_watch, set_civic_watch_status)
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import CivicWatchStatus

T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


def test_creates_an_active_watch(conn):
    watch = create_civic_watch(conn, "NZ election", "What changed today?", T0)
    assert watch.id is not None
    assert watch.status is CivicWatchStatus.ACTIVE
    assert watch.paused_at is None and watch.retired_at is None
    assert watch.is_schedulable


def test_subject_is_the_watch_identity(conn):
    create_civic_watch(conn, "NZ election", "Q1", T0)
    with pytest.raises(ValueError, match="already exists"):
        create_civic_watch(conn, "NZ election", "Q2", T1)
    assert len(list_civic_watches(conn)) == 1


def test_invalid_schedule_time_is_rejected_before_anything_is_written(conn):
    with pytest.raises(ValueError, match="HH:MM"):
        create_civic_watch(conn, "Bad", "Q", T0, schedule_time="25:00")
    assert list_civic_watches(conn) == []


def test_empty_subject_or_question_is_rejected(conn):
    with pytest.raises(ValueError):
        create_civic_watch(conn, "   ", "Q", T0)
    with pytest.raises(ValueError):
        create_civic_watch(conn, "S", "", T0)
    assert list_civic_watches(conn) == []


def test_lookup_by_subject_and_by_id(conn):
    watch = create_civic_watch(conn, "NZ election", "Q", T0)
    assert get_civic_watch_by_subject(conn, "NZ election") == watch
    assert get_civic_watch(conn, watch.id) == watch
    assert get_civic_watch_by_subject(conn, "nothing") is None
    assert get_civic_watch(conn, 999) is None


def test_pause_stamps_paused_at_and_resume_clears_it(conn):
    watch = create_civic_watch(conn, "S", "Q", T0)
    paused = pause_civic_watch(conn, watch.id, T1)
    assert paused.status is CivicWatchStatus.PAUSED
    assert paused.paused_at == T1
    resumed = resume_civic_watch(conn, watch.id, T1 + timedelta(hours=1))
    assert resumed.status is CivicWatchStatus.ACTIVE
    assert resumed.paused_at is None


def test_retiring_a_paused_watch_clears_paused_at_and_stamps_retired_at(conn):
    watch = create_civic_watch(conn, "S", "Q", T0)
    pause_civic_watch(conn, watch.id, T1)
    retired = retire_civic_watch(conn, watch.id, T1 + timedelta(hours=1))
    assert retired.status is CivicWatchStatus.RETIRED
    assert retired.paused_at is None
    assert retired.retired_at == T1 + timedelta(hours=1)


def test_a_retired_watch_can_never_be_revived(conn):
    watch = create_civic_watch(conn, "S", "Q", T0)
    retire_civic_watch(conn, watch.id, T1)
    for target in (CivicWatchStatus.ACTIVE, CivicWatchStatus.PAUSED, CivicWatchStatus.RETIRED):
        with pytest.raises(ValueError, match="invalid Civic Watch transition"):
            set_civic_watch_status(conn, watch.id, target, T1)
    assert get_civic_watch(conn, watch.id).status is CivicWatchStatus.RETIRED


def test_pausing_an_already_paused_watch_is_refused(conn):
    watch = create_civic_watch(conn, "S", "Q", T0)
    pause_civic_watch(conn, watch.id, T1)
    with pytest.raises(ValueError, match="invalid Civic Watch transition"):
        pause_civic_watch(conn, watch.id, T1)


def test_transitioning_a_missing_watch_raises(conn):
    with pytest.raises(ValueError, match="no Civic Watch"):
        pause_civic_watch(conn, 999, T0)


def test_only_active_watches_are_schedulable(conn):
    active = create_civic_watch(conn, "active", "Q", T0)
    paused = create_civic_watch(conn, "paused", "Q", T0)
    retired = create_civic_watch(conn, "retired", "Q", T0)
    pause_civic_watch(conn, paused.id, T1)
    retire_civic_watch(conn, retired.id, T1)

    assert [w.id for w in list_schedulable_civic_watches(conn)] == [active.id]
    assert [w.id for w in list_civic_watches(conn, CivicWatchStatus.PAUSED)] == [paused.id]
    assert len(list_civic_watches(conn)) == 3


def test_hard_delete_removes_the_watch(conn):
    watch = create_civic_watch(conn, "S", "Q", T0)
    assert hard_delete_civic_watch(conn, watch.id)
    assert get_civic_watch(conn, watch.id) is None
    assert not hard_delete_civic_watch(conn, watch.id)
