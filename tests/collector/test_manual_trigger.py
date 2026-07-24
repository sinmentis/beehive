# tests/collector/test_manual_trigger.py
import os

import pytest

from beehive.collector.manual_trigger import (
    MANUAL_FETCH_STALE_SECONDS,
    clear_stale_manual_triggers,
    complete_pending_manual_triggers,
    consume_pending_manual_trigger,
    consume_pending_manual_triggers,
    list_manual_trigger_states,
    request_channel_fetch,
    request_channel_fetch_batch,
)

_MARKER_NAME = "fetch_trigger_channel_id"


def _simulate_execstartpre_rename(data_dir: str) -> None:
    """In production this exact rename happens on the HOST via systemd's ExecStartPre=,
    before the manual container even starts (see the design doc) -- there is no Python
    equivalent in this module, so tests needing the full round trip do it explicitly here."""
    watched = os.path.join(data_dir, _MARKER_NAME)
    inflight = os.path.join(data_dir, f"{_MARKER_NAME}.inflight")
    os.replace(watched, inflight)


def test_request_writes_the_watched_marker_with_the_channel_id(tmp_path):
    request_channel_fetch(str(tmp_path), 42)
    marker_path = tmp_path / _MARKER_NAME
    assert marker_path.read_text() == "42"


def test_request_leaves_no_leftover_temp_file(tmp_path):
    request_channel_fetch(str(tmp_path), 7)
    leftovers = [
        p for p in os.listdir(tmp_path) if p.startswith(f".{_MARKER_NAME}.tmp-")
    ]
    assert leftovers == []


def test_batch_request_writes_each_unique_channel_id(tmp_path):
    request_channel_fetch_batch(str(tmp_path), [7, 42, 7])
    marker_path = tmp_path / _MARKER_NAME
    assert marker_path.read_text() == "7\n42"


def test_batch_request_rejects_an_empty_selection(tmp_path):
    with pytest.raises(ValueError, match="at least one"):
        request_channel_fetch_batch(str(tmp_path), [])


def test_full_round_trip_via_the_execstartpre_rename(tmp_path):
    request_channel_fetch(str(tmp_path), 42)
    _simulate_execstartpre_rename(str(tmp_path))
    assert consume_pending_manual_trigger(str(tmp_path)) == 42
    inflight_path = tmp_path / f"{_MARKER_NAME}.inflight"
    assert inflight_path.exists()

    complete_pending_manual_triggers(str(tmp_path))

    assert not inflight_path.exists()


def test_batch_round_trip_returns_every_requested_channel(tmp_path):
    request_channel_fetch_batch(str(tmp_path), [42, 7])
    _simulate_execstartpre_rename(str(tmp_path))
    assert consume_pending_manual_triggers(str(tmp_path)) == [42, 7]


def test_consume_returns_none_when_no_inflight_marker_exists(tmp_path):
    assert consume_pending_manual_trigger(str(tmp_path)) is None


def test_consume_returns_none_for_malformed_content_and_still_deletes_it(tmp_path):
    inflight_path = tmp_path / f"{_MARKER_NAME}.inflight"
    inflight_path.write_text("not-a-number")
    assert consume_pending_manual_trigger(str(tmp_path)) is None
    assert not inflight_path.exists()


def test_manual_trigger_states_distinguish_queued_and_running_requests(tmp_path):
    request_channel_fetch(str(tmp_path), 42)

    assert list_manual_trigger_states(str(tmp_path)) == {42: "queued"}

    _simulate_execstartpre_rename(str(tmp_path))

    assert list_manual_trigger_states(str(tmp_path)) == {42: "running"}


def test_manual_trigger_states_marks_old_inflight_requests_as_stale(tmp_path):
    request_channel_fetch(str(tmp_path), 42)
    _simulate_execstartpre_rename(str(tmp_path))
    inflight_path = tmp_path / f"{_MARKER_NAME}.inflight"
    os.utime(inflight_path, (100, 100))

    assert list_manual_trigger_states(
        str(tmp_path),
        now=100 + MANUAL_FETCH_STALE_SECONDS + 1,
    ) == {42: "stale"}


def test_clear_stale_only_removes_a_genuinely_stale_marker(tmp_path):
    request_channel_fetch(str(tmp_path), 42)
    _simulate_execstartpre_rename(str(tmp_path))
    inflight_path = tmp_path / f"{_MARKER_NAME}.inflight"
    os.utime(inflight_path, (100, 100))

    cleared = clear_stale_manual_triggers(
        str(tmp_path), now=100 + MANUAL_FETCH_STALE_SECONDS + 1
    )

    assert cleared is True
    assert not inflight_path.exists()


def test_clear_stale_leaves_a_running_marker_alone(tmp_path):
    request_channel_fetch(str(tmp_path), 42)
    _simulate_execstartpre_rename(str(tmp_path))
    inflight_path = tmp_path / f"{_MARKER_NAME}.inflight"

    cleared = clear_stale_manual_triggers(str(tmp_path))

    assert cleared is False
    assert inflight_path.exists()  # a fresh (running) marker is not touched


def test_clear_stale_is_a_noop_when_nothing_is_inflight(tmp_path):
    assert clear_stale_manual_triggers(str(tmp_path)) is False
