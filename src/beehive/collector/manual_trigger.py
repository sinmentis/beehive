"""Marker-file protocol for manual per-Channel fetch triggers.

The admin web route atomically writes one or more Channel ids to the watched marker.
The beehive-fetch-manual entrypoint reads the ALREADY-renamed .inflight file -- systemd's
ExecStartPre= moves the watched marker there, host-side, before the container starts, so a
container-boot failure cannot leave the watched path around to loop-retrigger the .path unit.
This module never touches the watched path from the read side or performs the rename itself.
"""
from __future__ import annotations

import os
import tempfile

_MARKER_NAME = "fetch_trigger_channel_id"


def _write_channel_ids(data_dir: str, channel_ids: list[int]) -> None:
    marker_path = os.path.join(data_dir, _MARKER_NAME)
    fd, tmp_path = tempfile.mkstemp(dir=data_dir, prefix=f".{_MARKER_NAME}.tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(str(channel_id) for channel_id in channel_ids))
        os.replace(tmp_path, marker_path)
    except BaseException:
        os.unlink(tmp_path)
        raise


def request_channel_fetch(data_dir: str, channel_id: int) -> None:
    """Atomically writes one Channel id to the watched marker."""
    _write_channel_ids(data_dir, [channel_id])


def request_channel_fetch_batch(data_dir: str, channel_ids: list[int]) -> None:
    """Atomically writes a deduplicated non-empty batch of Channel ids to the watched marker."""
    unique_ids = list(dict.fromkeys(channel_ids))
    if not unique_ids:
        raise ValueError("at least one Channel id is required")
    if any(channel_id <= 0 for channel_id in unique_ids):
        raise ValueError("Channel ids must be positive integers")
    _write_channel_ids(data_dir, unique_ids)


def consume_pending_manual_triggers(data_dir: str) -> list[int] | None:
    """Consumes the inflight marker and returns every valid requested Channel id.

    A missing or malformed marker is a clean no-op, never a fallback to the all-Channels sweep.
    """
    inflight_path = os.path.join(data_dir, f"{_MARKER_NAME}.inflight")
    try:
        with open(inflight_path) as f:
            content = f.read().strip()
    except FileNotFoundError:
        return None
    os.remove(inflight_path)
    if not content:
        return None
    try:
        channel_ids = [int(value) for value in content.splitlines()]
    except ValueError:
        return None
    if any(channel_id <= 0 for channel_id in channel_ids):
        return None
    return list(dict.fromkeys(channel_ids))


def consume_pending_manual_trigger(data_dir: str) -> int | None:
    """Compatibility wrapper returning the first id from a consumed marker."""
    channel_ids = consume_pending_manual_triggers(data_dir)
    return channel_ids[0] if channel_ids else None
