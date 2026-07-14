# tests/collector/test_manual_trigger.py
import os

from beehive.collector.manual_trigger import (
    consume_pending_manual_trigger,
    request_channel_fetch,
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
    leftovers = [p for p in os.listdir(tmp_path) if p.startswith(f".{_MARKER_NAME}.tmp-")]
    assert leftovers == []


def test_full_round_trip_via_the_execstartpre_rename(tmp_path):
    request_channel_fetch(str(tmp_path), 42)
    _simulate_execstartpre_rename(str(tmp_path))
    assert consume_pending_manual_trigger(str(tmp_path)) == 42
    inflight_path = tmp_path / f"{_MARKER_NAME}.inflight"
    assert not inflight_path.exists()


def test_consume_returns_none_when_no_inflight_marker_exists(tmp_path):
    assert consume_pending_manual_trigger(str(tmp_path)) is None


def test_consume_returns_none_for_malformed_content_and_still_deletes_it(tmp_path):
    inflight_path = tmp_path / f"{_MARKER_NAME}.inflight"
    inflight_path.write_text("not-a-number")
    assert consume_pending_manual_trigger(str(tmp_path)) is None
    assert not inflight_path.exists()
