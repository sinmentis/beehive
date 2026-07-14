"""Marker-file protocol for the manual per-Channel fetch trigger. request_channel_fetch
(called by the admin web route) atomically writes the watched marker;
consume_pending_manual_trigger (called by the beehive-fetch-manual container's Python
entrypoint) reads the ALREADY-renamed .inflight file -- systemd's ExecStartPre= moves the
watched marker there, host-side, before the container even starts, so a container-boot failure
can't leave the watched path around to loop-retrigger the .path unit forever. This module never
touches the watched path from the read side, and never performs the rename itself."""
from __future__ import annotations

import os
import tempfile

_MARKER_NAME = "fetch_trigger_channel_id"


def request_channel_fetch(data_dir: str, channel_id: int) -> None:
    """Atomically writes channel_id to <data_dir>/fetch_trigger_channel_id: write to a temp file
    in the same directory, then os.replace() it into place, so the systemd .path unit watching
    this exact path can never observe a partially-written file."""
    marker_path = os.path.join(data_dir, _MARKER_NAME)
    fd, tmp_path = tempfile.mkstemp(dir=data_dir, prefix=f".{_MARKER_NAME}.tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(str(channel_id))
        os.replace(tmp_path, marker_path)
    except BaseException:
        os.unlink(tmp_path)
        raise


def consume_pending_manual_trigger(data_dir: str) -> int | None:
    """Reads <data_dir>/fetch_trigger_channel_id.inflight -- the path ExecStartPre= already
    renamed the watched marker to, host-side, before this container started -- and deletes it.
    Returns the parsed Channel id, or None if the file is absent or its content isn't a plain
    integer. Both None cases are handled identically by the caller: a clean no-op, never a
    fallback to the all-Channels sweep (see the design doc's Error Handling section)."""
    inflight_path = os.path.join(data_dir, f"{_MARKER_NAME}.inflight")
    try:
        with open(inflight_path) as f:
            content = f.read().strip()
    except FileNotFoundError:
        return None
    os.remove(inflight_path)
    try:
        return int(content)
    except ValueError:
        return None
