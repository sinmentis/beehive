# Keep Civic on its own timer-driven units

ADR-0012 moved every background job into one always-on jobs worker and removed the timers. Civic
(the PartyMap Civic Watch run and its spool maintenance) stays outside it, as two oneshot
containers started by `beehive-civic.timer` and `beehive-civic-maintenance.timer`.

Civic has a different shape from the other jobs. It runs once a day, can take up to an hour, and
makes many AI calls. It also needs things no other job does: the host's PartyMap intake directory
bound at `/spool`, and a locked image (`deploy/Containerfile.civic`) with pinned base digests and
`uv sync --locked`, because its output is published outside Beehive. Putting it in the jobs worker
would give that worker the spool mount and Civic's memory and time budget, and a hung Civic run
could stall fetching, digests and reminders. That is the same reason ADR-0012 kept research in
its own worker.

The cost is two timers, and the dashboard's pause does not cover them yet: the workload
declaration still lists only the three always-on roles, and the dashboard's trusted binding would
need Civic roles first. Until then, pausing Civic means stopping its timers by hand.

Civic shares the database and its schema versioning with the rest of Beehive, so a release that
changes the schema must also roll the Civic image forward. The Civic image is built from the same
commit as the shared image.
