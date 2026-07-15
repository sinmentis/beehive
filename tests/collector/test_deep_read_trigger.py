import os

from beehive.collector.deep_read_trigger import (
    MARKER_NAME,
    consume_deep_read_wakeup,
    request_deep_read_worker,
)


def _simulate_execstartpre_rename(data_dir: str) -> None:
    os.replace(
        os.path.join(data_dir, MARKER_NAME),
        os.path.join(data_dir, f"{MARKER_NAME}.inflight"),
    )


def test_request_writes_atomic_wakeup_marker(tmp_path):
    request_deep_read_worker(str(tmp_path))

    assert (tmp_path / MARKER_NAME).read_text() == "pending\n"
    assert not any(path.name.startswith(f".{MARKER_NAME}.tmp-") for path in tmp_path.iterdir())


def test_repeated_request_replaces_existing_marker(tmp_path):
    request_deep_read_worker(str(tmp_path))
    request_deep_read_worker(str(tmp_path))

    assert (tmp_path / MARKER_NAME).read_text() == "pending\n"


def test_consume_removes_host_renamed_inflight_marker(tmp_path):
    request_deep_read_worker(str(tmp_path))
    _simulate_execstartpre_rename(str(tmp_path))

    assert consume_deep_read_wakeup(str(tmp_path)) is True
    assert not (tmp_path / f"{MARKER_NAME}.inflight").exists()


def test_reconciliation_run_needs_no_marker(tmp_path):
    assert consume_deep_read_wakeup(str(tmp_path)) is False
