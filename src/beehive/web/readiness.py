"""What `/readyz` asks the database before the process claims it can serve requests.

The dashboard control plane pauses and resumes this workload, and after a resume it decides
"is it back?" from one loopback HTTP call. A handler that returns a constant answers 200 from a
process whose `/data` volume was never mounted, whose SQLite file was replaced by an empty one,
or whose file is present but unreadable -- every page then 500s while the control plane reports
the workload healthy. So the check is a real bounded read through the project's own connection
seam (`db/connection.connect`), not a literal.

Three properties this deliberately keeps:

* **No mutation.** The probe only reads. It also refuses to open a missing file, because
  `sqlite3.connect` *creates* one, and a readiness check that quietly creates an empty database
  on the host's volume turns "the volume is not mounted" into "the volume is mounted and empty" --
  the same failure, now indistinguishable from a first boot.
* **Bounded.** The declared health timeout is 5 seconds, and `connect` sets `busy_timeout=5000`,
  which spends the entire budget waiting on one lock. The probe lowers its own busy timeout so a
  writer holding the lock produces a fast 503 rather than a hung request the prober times out on.
* **Nothing private in the response.** The body is a status and a fixed reason code. Detail goes
  to the log, where it is already the operator's, instead of to any local process that can reach
  the loopback port.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from beehive.db.connection import connect

_LOGGER = logging.getLogger(__name__)

# The tables every served request needs: a page render reads channels/items/sources, and deciding
# whether the caller is the Owner reads sessions. app_state carries the migration markers, so a
# database missing it has not been through init_schema at all. Deliberately a small core set
# rather than the whole of schema.sql -- readiness answers "can this process serve?", and a
# feature-specific table added next week should not be able to fail a resume.
REQUIRED_TABLES = ("app_state", "channels", "items", "sessions", "sources")
# The one table the probe actually reads a row from. sqlite_master alone can still be readable on
# a database whose pages are damaged, so the probe touches real content once.
_PROBE_TABLE = "channels"
# Well under the 5s health-check timeout: a slow answer is a failed answer to the prober, and a
# fast 503 is more useful than a request that outlives the check.
_PROBE_BUSY_TIMEOUT_MS = 1000

STATUS_READY = "ready"
STATUS_NOT_READY = "not_ready"
REASON_DATABASE_MISSING = "database_missing"
REASON_SCHEMA_INCOMPLETE = "schema_incomplete"
REASON_DATABASE_ERROR = "database_error"


@dataclass(frozen=True)
class Readiness:
    """The probe's verdict, plus the two things the HTTP layer needs to render it."""

    ready: bool
    reason: str | None = None

    @property
    def status_code(self) -> int:
        return 200 if self.ready else 503

    def body(self) -> dict[str, str]:
        if self.ready:
            return {"status": STATUS_READY}
        return {"status": STATUS_NOT_READY, "reason": self.reason or REASON_DATABASE_ERROR}


def check_readiness(db_path: str) -> Readiness:
    """Open the database, confirm the core schema is there, and read one row back."""
    if not Path(db_path).is_file():
        return Readiness(False, REASON_DATABASE_MISSING)

    conn: sqlite3.Connection | None = None
    try:
        conn = connect(db_path)
        conn.execute(f"PRAGMA busy_timeout={_PROBE_BUSY_TIMEOUT_MS}")
        placeholders = ", ".join("?" * len(REQUIRED_TABLES))
        present = {
            row[0]
            for row in conn.execute(
                f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({placeholders})",
                REQUIRED_TABLES,
            )
        }
        if present != set(REQUIRED_TABLES):
            _LOGGER.warning(
                "Readiness: missing tables %s", sorted(set(REQUIRED_TABLES) - present))
            return Readiness(False, REASON_SCHEMA_INCOMPLETE)
        conn.execute(f"SELECT 1 FROM {_PROBE_TABLE} LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        # No traceback: a broken workload is probed every few seconds, and a stack trace per
        # probe buries the first one. The type and message are the whole diagnosis anyway.
        _LOGGER.warning("Readiness: database unusable (%s: %s)", type(exc).__name__, exc)
        return Readiness(False, REASON_DATABASE_ERROR)
    finally:
        if conn is not None:
            conn.close()
    return Readiness(True)
