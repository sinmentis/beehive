"""The lock that lets one process at a time run background jobs against a database (ADR-0012).

The jobs worker holds it for as long as it runs; a one-shot `scripts/run_collector.py` job holds
it for as long as that command runs. So no Channel is ever fetched by two processes at once, which
is what keeps collection writes consistent: they carry no claim token of their own.

It is an flock on a file beside the database. The kernel drops it the moment its process ends,
however it ends, and a process that is merely stalled keeps it, so no other process can take over
from a stuck worker whose threads might still write. SQLite's WAL mode already needs the database
on a local disk, where flock also works between the containers that share the volume. Python
opens the file non-inheritable, so a subprocess (such as the Copilot CLI) never keeps it held.
"""
from __future__ import annotations

import fcntl
import os


class JobsLock:
    def __init__(self, db_path: str) -> None:
        self.path = f"{os.path.abspath(db_path)}.jobs.lock"
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        """Takes the lock if no other process holds it. True when this object holds it now."""
        if self._fd is not None:
            return True
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
