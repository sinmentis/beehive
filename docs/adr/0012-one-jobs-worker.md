# Run every background job in one always-on jobs worker

Fetching, "Fetch now", deep reads, digests and reminders used to run as oneshot containers. Five
systemd timers started most of them, and two path units watched marker files the web app wrote
for "Fetch now" and deep reads. That meant three ways to start work (timers, marker files, and the
research worker's database polling), 16 unit files, and about 550 container starts a day. It also
meant a local install did nothing when the Owner pressed **Fetch now**, because only the server
had the path units.

All of that now runs in one always-on process, the jobs worker (`scripts/run_jobs.py`), which polls
SQLite the way the research worker already did (ADR-0009). "Fetch now" is a `fetch_requests` row,
and a deep read was already a `deep_reads` row, so each piece of work is claimed with a lease and
a claim token, and a crashed worker's claims are recovered. The worker has five lanes (Fetch now,
the scheduled sweep, deep reads, email, reminders), each running one job at a time on its own
thread, so a slow digest can't delay a reminder and a manual fetch doesn't wait for a sweep. The
two fetch lanes never work on the same Channel at once, which the separate containers never
guaranteed.

Research keeps its own worker. A review of this design pointed out that merging them would let a
hung fetch or an out-of-memory deep read take down research runs and chat replies with it.

Only one process runs background jobs at a time. The jobs worker holds an flock on a file beside
the database for as long as it runs; a second worker waits, and the one-shot `run_collector` job
modes take the same lock or refuse to start. Together with the lanes never sharing a Channel, no
Channel is fetched twice at once, which is what keeps collection writes consistent, since they
carry no claim token of their own. A lease that expires would not do: a stalled worker would lose
it while its threads could still write. The kernel drops an flock only when its process ends,
which takes the threads with it.

A job cannot be killed from outside its thread, so each lane has a hard limit, the timeouts the
old containers had. A job past it makes the worker hand back its claims and exit, and systemd
restarts it, which is what the old container timeouts did. Abandoning a stuck thread instead would
leave it writing to the database after its work was handed to someone else. The stuck job's own
claim counts as a failed try, so one hopeless request cannot restart the worker forever. Article
fetches keep their 20-second budget on the worker's threads, where the SIGALRM-based limit never
worked: a timer shuts down the socket a fetch is stuck on.

The worker holds the email secret, which the fetch containers never had, so a Channel's failure
alert email now really goes out. It is limited to one every six hours per Channel, because a
failing ranking fails again every 15 minutes.

The research reconcile timer went too: the research worker already reconciles at start-up and
every minute. What remains is three always-on units and the host's backup timer. Rolling back past
this change needs the old unit files as well as the old image.
