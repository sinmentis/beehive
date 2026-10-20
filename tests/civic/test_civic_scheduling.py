from datetime import timedelta

import pytest

from beehive.civic.scheduling import (
    CivicGlobalWatchConfigurationError,
    CivicScheduleDisposition,
    schedule_due_civic_runs,
    schedule_partymap_civic_run,
)
from beehive.db.civic_runs import (
    claim_civic_run,
    complete_civic_run,
    get_civic_run_for_slot,
    list_civic_runs,
)
from beehive.db.connection import connect, init_schema
from beehive.db.civic_vocabulary import (
    add_vocabulary_term,
    create_vocabulary_version,
    publish_vocabulary_version,
)
from beehive.db.civic_watches import create_civic_watch
from beehive.domain.civic import CivicTermCategory, CivicTermRole
from beehive.domain.civic import CivicRunStatus
from tests.civic.civic_worker_support import T0, create_partymap_watch


def test_due_slot_is_timezone_aware_and_idempotent(tmp_path):
    conn = connect(str(tmp_path / "schedule.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn)

    first = schedule_due_civic_runs(conn, T0)
    second = schedule_due_civic_runs(conn, T0)

    assert first[0].disposition is CivicScheduleDisposition.ENQUEUED
    assert second[0].disposition is CivicScheduleDisposition.EXISTING
    assert first[0].run.id == second[0].run.id
    assert first[0].slot_at == T0
    assert first[0].slot_at.tzinfo is not None
    assert get_civic_run_for_slot(conn, watch.id, T0).id == first[0].run.id


def test_active_older_slot_blocks_then_latest_slot_catches_up(tmp_path):
    conn = connect(str(tmp_path / "backlog.db"))
    init_schema(conn)
    create_partymap_watch(conn)
    first = schedule_due_civic_runs(conn, T0)[0].run

    tomorrow = T0 + timedelta(days=1)
    blocked = schedule_due_civic_runs(conn, tomorrow)[0]
    assert blocked.disposition is CivicScheduleDisposition.BLOCKED_ACTIVE_RUN
    assert blocked.blocking_run.id == first.id

    lease = claim_civic_run(conn, first.id, T0, 60, 600)
    assert complete_civic_run(
        conn, first.id, lease.run.claim_token, CivicRunStatus.COMPLETED, T0)
    caught_up = schedule_due_civic_runs(conn, tomorrow)[0]
    assert caught_up.disposition is CivicScheduleDisposition.ENQUEUED
    assert caught_up.slot_at == tomorrow


def test_watch_created_after_today_slot_waits_for_tomorrow(tmp_path):
    conn = connect(str(tmp_path / "not-due.db"))
    init_schema(conn)
    created_at = T0 + timedelta(hours=2)
    watch = create_civic_watch(
        conn, "green", "Q", created_at, schedule_time="06:30")
    vocabulary = create_vocabulary_version(conn, watch.id, created_at)
    add_vocabulary_term(
        conn, vocabulary.id, "housing", CivicTermCategory.TOPIC,
        CivicTermRole.REQUIRED, created_at)
    publish_vocabulary_version(conn, vocabulary.id, created_at)

    result = schedule_due_civic_runs(conn, created_at)[0]
    assert result.disposition is CivicScheduleDisposition.NOT_DUE


def test_partymap_scheduler_requires_one_global_published_watch(tmp_path):
    conn = connect(str(tmp_path / "global.db"))
    init_schema(conn)
    with pytest.raises(CivicGlobalWatchConfigurationError, match="exactly one active"):
        schedule_partymap_civic_run(conn, T0)

    create_civic_watch(conn, "partymap", "Q", T0, schedule_time="06:30")
    with pytest.raises(CivicGlobalWatchConfigurationError, match="no published vocabulary"):
        schedule_partymap_civic_run(conn, T0)


def test_partymap_scheduler_fails_before_enqueuing_party_per_watch_layout(tmp_path):
    conn = connect(str(tmp_path / "many.db"))
    init_schema(conn)
    global_watch = create_partymap_watch(conn)
    party_watch = create_civic_watch(
        conn, "national", "Q", T0, schedule_time="06:30")
    vocabulary = create_vocabulary_version(conn, party_watch.id, T0)
    add_vocabulary_term(
        conn, vocabulary.id, "housing", CivicTermCategory.TOPIC,
        CivicTermRole.REQUIRED, T0)
    publish_vocabulary_version(conn, vocabulary.id, T0)

    with pytest.raises(CivicGlobalWatchConfigurationError, match="found 2"):
        schedule_partymap_civic_run(conn, T0)
    assert list_civic_runs(conn, global_watch.id) == []
    assert list_civic_runs(conn, party_watch.id) == []


def test_partymap_scheduler_rejects_a_party_labeled_watch(tmp_path):
    conn = connect(str(tmp_path / "wrong-label.db"))
    init_schema(conn)
    watch = create_civic_watch(
        conn, "national", "Q", T0, schedule_time="06:30")
    vocabulary = create_vocabulary_version(conn, watch.id, T0)
    add_vocabulary_term(
        conn, vocabulary.id, "housing", CivicTermCategory.TOPIC,
        CivicTermRole.REQUIRED, T0)
    publish_vocabulary_version(conn, vocabulary.id, T0)

    with pytest.raises(CivicGlobalWatchConfigurationError, match="'partymap'"):
        schedule_partymap_civic_run(conn, T0)
    assert list_civic_runs(conn, watch.id) == []


def test_partymap_scheduler_requires_the_approved_auckland_slot(tmp_path):
    conn = connect(str(tmp_path / "wrong-slot.db"))
    init_schema(conn)
    watch = create_civic_watch(
        conn, "partymap", "Q", T0, schedule_time="07:00")
    vocabulary = create_vocabulary_version(conn, watch.id, T0)
    add_vocabulary_term(
        conn, vocabulary.id, "housing", CivicTermCategory.TOPIC,
        CivicTermRole.REQUIRED, T0)
    publish_vocabulary_version(conn, vocabulary.id, T0)

    with pytest.raises(CivicGlobalWatchConfigurationError, match="06:30"):
        schedule_partymap_civic_run(conn, T0)
    assert list_civic_runs(conn, watch.id) == []
