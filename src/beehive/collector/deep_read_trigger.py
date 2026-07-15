"""Marker-file wakeup for queued deep-read jobs.

SQLite is the durable queue. The marker only asks systemd to start the worker
quickly; a reconciliation timer also starts the worker when the marker is lost.
"""
from __future__ import annotations

import os
import tempfile

MARKER_NAME = "deep_read_trigger"


def request_deep_read_worker(data_dir: str) -> None:
    """Atomically create the watched marker after a deep-read request is committed."""
    marker_path = os.path.join(data_dir, MARKER_NAME)
    fd, tmp_path = tempfile.mkstemp(dir=data_dir, prefix=f".{MARKER_NAME}.tmp-")
    try:
        with os.fdopen(fd, "w") as marker:
            marker.write("pending\n")
        os.replace(tmp_path, marker_path)
    except BaseException:
        os.unlink(tmp_path)
        raise


def consume_deep_read_wakeup(data_dir: str) -> bool:
    """Delete the host-renamed inflight marker and report whether one existed."""
    inflight_path = os.path.join(data_dir, f"{MARKER_NAME}.inflight")
    try:
        os.remove(inflight_path)
    except FileNotFoundError:
        return False
    return True
