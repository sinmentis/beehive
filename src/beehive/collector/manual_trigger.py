"""Marker-file protocol for manual per-Channel fetch triggers.

The admin web route atomically writes one or more Channel ids to the watched marker.
The beehive-fetch-manual entrypoint reads the ALREADY-renamed .inflight file -- systemd's
ExecStartPre= moves the watched marker there, host-side, before the container starts, so a
container-boot failure cannot leave the watched path around to loop-retrigger the .path unit.
The admin UI reads both paths non-destructively to show queued/running state. This module never
performs the watched-to-inflight rename itself.
"""

from __future__ import annotations

import os
import tempfile
import time

_MARKER_NAME = "fetch_trigger_channel_id"
MANUAL_FETCH_STALE_SECONDS = 65 * 60


def _marker_path(data_dir: str, *, inflight: bool = False) -> str:
    suffix = ".inflight" if inflight else ""
    return os.path.join(data_dir, f"{_MARKER_NAME}{suffix}")


def _read_channel_ids(path: str) -> list[int] | None:
    try:
        with open(path) as f:
            content = f.read().strip()
    except FileNotFoundError:
        return None
    if not content:
        return None
    try:
        channel_ids = [int(value) for value in content.splitlines()]
    except ValueError:
        return None
    if any(channel_id <= 0 for channel_id in channel_ids):
        return None
    return list(dict.fromkeys(channel_ids))


def _write_channel_ids(data_dir: str, channel_ids: list[int]) -> None:
    marker_path = _marker_path(data_dir)
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

    A valid marker remains in place while the worker runs so the admin UI can report that the
    request is in progress. The worker removes it with complete_pending_manual_triggers().
    A missing or malformed marker is a clean no-op, never a fallback to the all-Channels sweep.
    """
    inflight_path = _marker_path(data_dir, inflight=True)
    channel_ids = _read_channel_ids(inflight_path)
    if channel_ids is None:
        try:
            os.remove(inflight_path)
        except FileNotFoundError:
            pass
    return channel_ids


def complete_pending_manual_triggers(data_dir: str) -> None:
    """Clears the running marker after the worker finishes or fails."""
    try:
        os.remove(_marker_path(data_dir, inflight=True))
    except FileNotFoundError:
        pass


def clear_stale_manual_triggers(data_dir: str, *, now: float | None = None) -> bool:
    """Owner-driven recovery for an inflight marker a crashed/killed worker never cleared.

    Only acts when the marker is actually STALE (older than MANUAL_FETCH_STALE_SECONDS, as
    list_manual_trigger_states already decides) -- a genuinely running fetch is left alone. Reuses
    the existing marker helpers and never touches the marker protocol itself: the staleness verdict
    comes from list_manual_trigger_states and the removal from complete_pending_manual_triggers.
    Returns True when a stale marker was cleared, False when there was nothing stale to clear.
    """
    states = list_manual_trigger_states(data_dir, now=now)
    if "stale" not in states.values():
        return False
    complete_pending_manual_triggers(data_dir)
    return True


def list_manual_trigger_states(
    data_dir: str,
    *,
    now: float | None = None,
) -> dict[int, str]:
    """Returns queued/running/stale state without consuming either marker."""
    states: dict[int, str] = {}
    inflight_path = _marker_path(data_dir, inflight=True)
    inflight_ids = _read_channel_ids(inflight_path) or []
    if inflight_ids:
        try:
            age_seconds = (time.time() if now is None else now) - os.path.getmtime(
                inflight_path
            )
        except FileNotFoundError:
            inflight_ids = []
        else:
            state = "stale" if age_seconds > MANUAL_FETCH_STALE_SECONDS else "running"
            states.update(dict.fromkeys(inflight_ids, state))

    for channel_id in _read_channel_ids(_marker_path(data_dir)) or []:
        if states.get(channel_id) != "running":
            states[channel_id] = "queued"
    return states


def consume_pending_manual_trigger(data_dir: str) -> int | None:
    """Compatibility wrapper returning the first id from a consumed marker."""
    channel_ids = consume_pending_manual_triggers(data_dir)
    return channel_ids[0] if channel_ids else None
