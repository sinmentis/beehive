# SQLite (WAL mode), not DuckDB

Beehive has two long-lived processes that both need to write the same logical database: the
collector (new Items, on a timer) and the always-on web app (Votes, read/unread state, Channel/
Source CRUD, admin logins). DuckDB is single-writer at the process level — it refuses any second
connection, even read-only, to a file another process holds open for writing. Working around that
would force one process to publish a read-only copy for the other, or add a write-queue/IPC seam
between the two containers purely to satisfy the engine — solving a problem SQLite doesn't have,
since Beehive's web app is itself a first-class writer, not just a reader.

SQLite in WAL mode supports multiple processes writing the same file directly, with readers never
blocked and writes serialized transparently. Both writers here are low-frequency and bursty (a
fetch cycle every few hours; occasional human clicks), so real contention is negligible. This lets
the collector and web containers mount and write one shared SQLite file with no publish dance, and
gives `ON DELETE CASCADE` (the Channel-deletion behavior) and JSON1 (for per-source-type metadata)
for free. DuckDB's genuine strength — fast analytical scans — is not exercised anywhere in this
app's query shape (the heaviest query is "list one Channel's Items, newest first, filtered by
read-state").

SQLite is therefore selected for concurrent, lightweight application reads and writes. Recorded so
a future contributor doesn't "fix" this toward a heavier analytical engine the workload never
needs.
