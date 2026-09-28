import json
from datetime import datetime, timedelta, timezone

import pytest

from beehive.ai import model_catalog as mc
from beehive.ai.model_catalog import (
    BUILTIN_MODELS,
    CATALOG_KEY,
    REFRESH_KEY,
    ListedModel,
    ModelOption,
    RefreshError,
    RefreshStatus,
    available_models,
    claim_refresh,
    complete_refresh,
    fail_refresh,
    load_catalog,
    load_refresh_state,
    normalize_models,
    request_refresh,
    requeue_refresh,
)
from beehive.db import app_state
from beehive.db.connection import connect, init_schema

T0 = datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)
LISTED = [
    ListedModel("claude-haiku-4.5", "Claude Haiku 4.5", "enabled"),
    ListedModel("auto", "Auto", None),
    ListedModel("gpt-6-sol", "GPT-6 Sol", "enabled"),
]


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "catalog.db"))
    init_schema(connection)
    yield connection
    connection.close()


def _ids(models):
    return [model.model_id for model in models]


# -- normalize_models ------------------------------------------------------------------------

def test_only_models_the_account_can_use_are_kept():
    listed = [
        ListedModel("auto", "Auto", None),
        ListedModel("on", "On", "enabled"),
        ListedModel("off", "Off", "disabled"),
        ListedModel("later", "Later", "unconfigured"),
    ]
    assert _ids(normalize_models(listed)) == ["auto", "on"]


def test_malformed_and_duplicate_ids_are_dropped():
    listed = [
        ListedModel("good-1", "First"),
        ListedModel("good-1", "Second"),
        ListedModel("has space", "Bad"),
        ListedModel("", "Empty"),
        ListedModel("-leading", "Bad"),
        ListedModel("x" * 101, "Too long"),
        ListedModel("vendor:model.v2_x", "Allowed punctuation"),
    ]
    models = normalize_models(listed)
    assert _ids(models) == ["vendor:model.v2_x", "good-1"]
    assert models[1].display_name == "First"


def test_names_are_cleaned_and_bounded():
    models = normalize_models([
        ListedModel("a", "  Spaced\t\nout  name "),
        ListedModel("b", "\x00\x1b"),
        ListedModel("c", "N" * 200),
    ])
    names = {model.model_id: model.display_name for model in models}
    assert names == {"a": "Spaced out name", "b": "b", "c": "N" * 80}


def test_auto_comes_first_then_natural_name_order():
    listed = [ListedModel(model_id, name) for model_id, name in [
        ("gpt-5.10", "GPT-5.10"), ("grok", "Grok 4.5"), ("gpt-5.9", "GPT-5.9"),
        ("auto", "Auto"), ("claude", "Claude Opus 5"), ("gpt-5-mini", "GPT-5 mini")]]
    assert _ids(normalize_models(listed)) == [
        "auto", "claude", "gpt-5-mini", "gpt-5.9", "gpt-5.10", "grok"]


# -- the stored list --------------------------------------------------------------------------

def test_the_builtin_list_stands_in_before_the_first_refresh(conn):
    assert load_catalog(conn) is None
    assert available_models(conn) == BUILTIN_MODELS


def test_an_unreadable_stored_list_falls_back_to_the_builtin_one(conn, capsys):
    app_state.set(conn, CATALOG_KEY, "{not json")
    assert available_models(conn) == BUILTIN_MODELS
    assert "unreadable" in capsys.readouterr().out

    app_state.set(conn, CATALOG_KEY, json.dumps(
        {"refreshed_at": "2026-09-29T00:00:00", "models": [{"id": "a", "name": "A"}]}))
    assert load_catalog(conn) is None  # a time without its UTC offset is not trusted


# -- request_refresh --------------------------------------------------------------------------

def test_a_request_is_queued_once_while_it_is_pending(conn):
    first = request_refresh(conn, T0)
    again = request_refresh(conn, T0 + timedelta(seconds=30))

    assert first.status is RefreshStatus.QUEUED
    assert again.requested_at == T0

    claim_refresh(conn, T0 + timedelta(seconds=31))
    while_running = request_refresh(conn, T0 + timedelta(seconds=32))
    assert while_running.status is RefreshStatus.RUNNING


def test_a_request_after_a_finished_or_dead_refresh_queues_a_new_one(conn):
    claim = claim_refresh(conn, T0)
    complete_refresh(conn, claim, T0, LISTED)
    assert request_refresh(conn, T0 + timedelta(minutes=1)).status is RefreshStatus.QUEUED

    claim_refresh(conn, T0 + timedelta(minutes=1))
    after_crash = request_refresh(conn, T0 + timedelta(minutes=10))
    assert after_crash.status is RefreshStatus.QUEUED
    assert after_crash.requested_at == T0 + timedelta(minutes=10)


# -- claim_refresh ----------------------------------------------------------------------------

def test_the_first_poll_claims_a_daily_refresh(conn):
    claim = claim_refresh(conn, T0)

    assert claim is not None and claim.requested is False
    state = load_refresh_state(conn)
    assert state.status is RefreshStatus.RUNNING
    assert state.claim == claim.claim
    assert claim_refresh(conn, T0 + timedelta(seconds=5)) is None


def test_a_queued_request_is_claimed_as_requested(conn):
    request_refresh(conn, T0)
    claim = claim_refresh(conn, T0 + timedelta(seconds=5))

    assert claim is not None and claim.requested is True
    assert load_refresh_state(conn).requested_at == T0


def test_a_refresh_a_dead_worker_left_running_is_claimed_again(conn):
    request_refresh(conn, T0)
    dead = claim_refresh(conn, T0)
    assert claim_refresh(conn, T0 + timedelta(minutes=2)) is None

    again = claim_refresh(conn, T0 + mc.RUNNING_STALE_AFTER + timedelta(seconds=1))
    assert again is not None and again.claim != dead.claim
    assert again.requested is True


def test_daily_refreshes_wait_a_day_after_any_attempt(conn):
    claim = claim_refresh(conn, T0)
    fail_refresh(conn, claim, T0, RefreshError.FAILED, "boom")

    assert claim_refresh(conn, T0 + timedelta(hours=23)) is None
    assert claim_refresh(conn, T0 + mc.AUTO_REFRESH_INTERVAL) is not None


def test_nothing_due_leaves_the_stored_state_untouched(conn):
    claim = claim_refresh(conn, T0)
    complete_refresh(conn, claim, T0, LISTED)
    before = app_state.get(conn, REFRESH_KEY)

    assert claim_refresh(conn, T0 + timedelta(hours=1)) is None
    assert app_state.get(conn, REFRESH_KEY) == before


# -- complete / fail / requeue ----------------------------------------------------------------

def test_completing_stores_the_list_and_counts_the_changes(conn):
    request_refresh(conn, T0)
    claim = claim_refresh(conn, T0)
    state = complete_refresh(conn, claim, T0 + timedelta(seconds=7), LISTED)

    assert state.status is RefreshStatus.DONE
    assert state.model_count == 3
    assert state.added == ()  # all three are in the built-in list the Owner saw before
    assert set(state.removed) == set(_ids(BUILTIN_MODELS)) - {"auto", "claude-haiku-4.5", "gpt-6-sol"}
    assert state.claim is None
    catalog = load_catalog(conn)
    assert catalog.refreshed_at == T0 + timedelta(seconds=7)
    assert catalog.models == (
        ModelOption("auto", "Auto"), ModelOption("claude-haiku-4.5", "Claude Haiku 4.5"),
        ModelOption("gpt-6-sol", "GPT-6 Sol"))

    a_day_later = T0 + timedelta(seconds=7) + mc.AUTO_REFRESH_INTERVAL
    second = complete_refresh(
        conn, claim_refresh(conn, a_day_later), a_day_later,
        [*LISTED, ListedModel("brand-new", "Brand New")])
    assert second.added == ("brand-new",)
    assert second.removed == ()


def test_a_superseded_claim_cannot_overwrite_the_newer_result(conn):
    request_refresh(conn, T0)
    stale = claim_refresh(conn, T0)
    newer = claim_refresh(conn, T0 + mc.RUNNING_STALE_AFTER + timedelta(seconds=1))

    assert complete_refresh(conn, stale, T0 + timedelta(minutes=4), LISTED) is None
    assert fail_refresh(conn, stale, T0, RefreshError.FAILED, "late") is None
    assert load_catalog(conn) is None
    assert load_refresh_state(conn).claim == newer.claim


def test_a_list_with_nothing_usable_fails_and_keeps_the_previous_list(conn):
    claim = claim_refresh(conn, T0)
    state = complete_refresh(conn, claim, T0, [ListedModel("off", "Off", "disabled")])

    assert state.status is RefreshStatus.FAILED
    assert state.error is RefreshError.EMPTY
    assert load_catalog(conn) is None


def test_a_failure_records_a_short_one_line_reason(conn):
    claim = claim_refresh(conn, T0)
    state = fail_refresh(conn, claim, T0, RefreshError.FAILED, "RuntimeError:\n  " + "x" * 400)

    assert state.status is RefreshStatus.FAILED
    assert state.error_detail.startswith("RuntimeError: xxx")
    assert len(state.error_detail) == 300
    assert load_refresh_state(conn) == state


def test_a_handed_back_refresh_is_queued_again(conn):
    daily = claim_refresh(conn, T0)
    requeue_refresh(conn, daily, T0 + timedelta(seconds=3))
    state = load_refresh_state(conn)
    assert state.status is RefreshStatus.QUEUED
    assert state.requested_at == T0 + timedelta(seconds=3)

    requested = claim_refresh(conn, T0 + timedelta(seconds=5))
    requeue_refresh(conn, requested, T0 + timedelta(seconds=9))
    assert load_refresh_state(conn).requested_at == T0 + timedelta(seconds=3)


def test_pending_waiting_and_stale_thresholds():
    queued = mc.RefreshState(RefreshStatus.QUEUED, requested_at=T0)
    assert queued.is_pending(T0 + timedelta(hours=1))
    assert not queued.is_waiting(T0 + mc.QUEUED_WAIT_WARNING)
    assert queued.is_waiting(T0 + mc.QUEUED_WAIT_WARNING + timedelta(seconds=1))

    running = mc.RefreshState(RefreshStatus.RUNNING, started_at=T0, claim="c")
    assert running.is_pending(T0 + mc.RUNNING_STALE_AFTER)
    assert running.is_stale(T0 + mc.RUNNING_STALE_AFTER + timedelta(seconds=1))
    assert not running.is_pending(T0 + mc.RUNNING_STALE_AFTER + timedelta(seconds=1))
