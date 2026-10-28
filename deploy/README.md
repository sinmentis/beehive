# Beehive deployment (rootless Podman + Quadlet)

Beehive runs on a single rootless-Podman host as one shared image (`../Containerfile`). Each
process selects its role through a Quadlet unit's `Exec=`:

- an always-on web app (the reading pages and `/admin/*`),
- an always-on jobs worker (ADR-0012) that runs every Channel's scheduled fetch and AI ranking,
  "Fetch now", article deep reads, Email Group digests with Research-completion emails, and
  Tracker reminders, and
- an always-on durable Research worker (Research Runs + Research Chat replies, ADR-0009).

Apart from the host's nightly backup timer and the two PartyMap Civic timers, there are no timers
or path units: each worker polls its own queues and schedules in SQLite. Civic is the documented
exception (ADR-0013): a daily 06:30 Pacific/Auckland Civic Watch run and a five-minute spool
maintenance job, both oneshot containers on their own locked image. See
[Civic (PartyMap) worker](#civic-partymap-worker-adr-0013).

The read surfaces are served directly and are public unless the Owner sets **Who can read** to
private (ADR-0011); `/admin/*` and all write actions are always gated by the app's own password
login (ADR-0003, ADR-0005), so no host-level identity gateway is required. If
you want to expose Beehive beyond localhost, put any reverse proxy or tunnel (nginx, Caddy,
cloudflared, etc.) in front of the web container's published port. The examples below use
placeholder values; replace them with your own.

Dynamic responses use `Cache-Control: private, no-store` and `Vary: Cookie` because public pages
show owner-only controls when an authenticated session is present. Do not override these headers in
the reverse proxy.

## Files

| Path | Purpose |
|------|---------|
| `../Containerfile` | Single shared image for every role; `ENTRYPOINT` is bare `python`, each unit supplies its own `-m scripts...` invocation |
| `quadlet/beehive-data.volume` | Named Podman volume backing `/data` (the SQLite DB), shared by all containers below |
| `quadlet/beehive-web.container` | Always-on web app — `PublishPort=127.0.0.1:8095:8000`, `Restart=always` |
| `quadlet/beehive-jobs.container` | Always-on jobs worker (ADR-0012) — scheduled fetching, "Fetch now", deep reads, digests and reminders, `Restart=always` |
| `quadlet/beehive-research.container` | Always-on durable Research worker — bounded Research Run + Research Chat pools (ADR-0009), `Restart=always` |
| `quadlet/beehive-civic.container` + `.timer` | Daily 06:30 Pacific/Auckland Civic Watch run and canonical inbox write; binds the host intake directory at `/spool` (ADR-0013) |
| `quadlet/beehive-civic-maintenance.container` + `.timer` | Five-minute Civic lease recovery, sealed-envelope replay and outcome processing over the same intake bind; receives no AI credential |
| `Containerfile.civic` | Locked image for the two Civic units (`localhost/beehive:civic`), pinned base digests and `uv sync --locked` |
| `systemd/beehive-backup.service` + `.timer`, `backup/` | Nightly host-side SQLite backup with a restore drill on every run (see [Backups](#backups)) |

The web container publishes to `127.0.0.1` only, so the app is reachable from the host's loopback
and from whatever reverse proxy or tunnel you place in front of it, not from the public internet
directly.

## Logging

Every container unit sets `LogDriver=passthrough`, so the process's output reaches the journal
once, through the unit itself. With Podman's default `journald` log driver an attached
`podman run` logged every line twice. The units also set `GlobalArgs=--events-backend=file`,
which keeps Podman's own container lifecycle events (create, start, died, remove) out of the
journal. Those added about 7 lines per container start, roughly 9,000 lines a day, and pushed the
user journal past its size cap until it only reached back about a day. The events stay available
through `podman events`. Read the jobs worker's output with
`journalctl --user -u beehive-jobs.service`; `podman logs` does not apply to passthrough
containers.

## Backups

`deploy/backup/beehive-backup.sh` takes a nightly online backup of the SQLite database with the
`sqlite3` CLI on the host. SQLite's backup API copies a consistent snapshot while every container
keeps writing, so nothing is stopped. Archives go to `~/backups/beehive` (point it at a different
disk from the Podman volume; `BEEHIVE_BACKUP_DIR` overrides it). Every run is also a restore drill:
`verify-beehive-backup.py` decompresses the new archive into a scratch file and checks
`integrity_check`, the core tables, and that the item count sits between the live counts taken just
before and after the copy. Only then is the archive renamed into place, so a file matching
`beehive-*.db.gz` is always complete. Retention is 14 days, and the newest 3 are always kept.

Install or update it:

```bash
install -m 0755 deploy/backup/beehive-backup.sh deploy/backup/verify-beehive-backup.py ~/.local/bin/
install -m 0644 deploy/systemd/beehive-backup.service deploy/systemd/beehive-backup.timer \
  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now beehive-backup.timer
systemctl --user start beehive-backup.service   # take one now; see journalctl -u beehive-backup
```

Restore:

1. Pause Beehive (the dashboard's pause, or stop `beehive-web.service`, `beehive-jobs.service`
   and `beehive-research.service`).
2. Move the current database aside, keeping its WAL files with it:

   ```bash
   VOL=~/.local/share/containers/storage/volumes/beehive-data/_data
   for f in beehive.db beehive.db-wal beehive.db-shm; do
     [ -e "$VOL/$f" ] && mv "$VOL/$f" "$VOL/$f.before-restore"
   done
   gunzip -c ~/backups/beehive/beehive-YYYYMMDD-HHMMSS.db.gz > "$VOL/beehive.db"
   ```

3. Resume. The first process to start migrates an older archive to the current schema version.
   A build older than the archive's compatible schema version refuses to start rather than write
   to it (see [Releases](#releases)).

## Releases

Release only committed code, identified by its git SHA. `latest` is just a pointer that the
Quadlet units follow; the image a unit ran is always traceable through its
`org.opencontainers.image.revision` label.

```bash
TAG=$(deploy/release.sh build)       # refuses a dirty working tree; tags localhost/beehive:<sha>
deploy/release.sh migrate "$TAG"     # takes a backup, then migrates the schema exactly once
deploy/release.sh promote "$TAG"     # old image becomes :rollback, :latest -> $TAG, waits for /readyz
deploy/release.sh prune              # keep only :latest and :rollback among SHA-tagged images
```

The schema carries a version (`PRAGMA user_version`) and the oldest compatible code version, so
the order matters: migrate first, then promote. Every process still runs the version-gated
`init_schema` on start, which is a single PRAGMA read once the file is current. A build that is
older than the database's compatible version refuses to start instead of writing to it
(`SchemaTooNewError`). Additive migrations keep the compatible version where it was, so rolling
back is just a swap of `:latest` and `:rollback` (it restarts the always-on units and waits for
`/readyz` the same way):

```bash
deploy/release.sh promote rollback
```

`prune` only untags and removes Beehive's own SHA-tagged images; it never runs a host-wide
`podman image prune`, because other projects share the image store.

## One-time upgrade tasks

The reading UX follow-up uses the existing schema 7. After release, configure **Email groups >
Website links** with the site's base HTTP(S) address to enable email return links. This is the
`site_url` setting, not a value inferred from request headers. Leaving it blank preserves ordinary
outbound item links but omits site/channel/brief navigation.

The non-admin reliability fixes require schema version 7. The normal backed-up migration adds
`deep_reads.result_completed_at` and backfills existing successful briefs without changing their
content. Update the web app and jobs worker together to retain saved briefs during regeneration.

The task-based admin requires schema version 6. Use the normal backed-up release migration above.
It adds nullable draft setup state and durable rule-operation tables; existing channel IDs,
source configuration, interactions, schedules and email membership remain unchanged. Upgrade the
web app and jobs worker together: an older worker will not process the new rule queue.

These only apply to an install from an older release. New installs never need them.

### Move to the jobs worker (ADR-0012)

Releases before the jobs worker ran each job as a oneshot container started by a timer or a path
unit. After building and migrating the new image, and before `promote`, swap the units. Keep the
values your old units had: copy `DIGEST_EMAIL_TO`, `DIGEST_EMAIL_FROM` and the two `Secret=`
names from your installed `beehive-digest.container` into `beehive-jobs.container` first.

```bash
OLD="fetch digest auction-reminders deep-read research-reconcile"
for name in $OLD; do systemctl --user disable --now "beehive-$name.timer"; done
systemctl --user disable --now beehive-fetch-manual.path beehive-deep-read.path
# Let a job that is already running finish, then remove the old units.
for name in $OLD fetch-manual; do
  while systemctl --user is-active --quiet "beehive-$name.service"; do sleep 5; done
  rm -f ~/.config/containers/systemd/beehive-$name.container \
        ~/.config/systemd/user/beehive-$name.timer ~/.config/systemd/user/beehive-$name.path
done
cp deploy/quadlet/beehive-jobs.container ~/.config/containers/systemd/
systemctl --user daemon-reload
rm -f ~/.local/share/containers/storage/volumes/beehive-data/_data/*_trigger*
```

Then run `deploy/release.sh promote <tag>`, which starts `beehive-jobs.service` with the other
two. A "Fetch now" request or deep read queued under the old units is not lost: deep reads were
already SQLite rows, and the admin simply offers "Fetch now" again. If a dashboard or other tool
starts and stops Beehive's units by name, point it at `beehive-web`, `beehive-research` and
`beehive-jobs` (see [Dashboard lifecycle contract](#dashboard-lifecycle-contract)).

To roll back past this release, `promote rollback` alone is not enough, because the old image has
no `scripts/run_jobs.py`: stop and remove `beehive-jobs.container`, then reinstall the old
release's units with `git checkout <old-tag> -- deploy/quadlet` and the install steps below.

### Rewrite existing unread summaries

After upgrading from topic-description summaries, existing ranked and unread items can be rewritten
to the conclusion-first format. The rewrite calls the AI, so run it in `beehive-research`, the
always-on container that has the Copilot token. Snapshot the item high-water mark before
deployment and keep it constant for every command in the run:

```bash
HIGH_WATER_ITEM_ID="$(
  podman exec beehive-research python -c \
    'import sqlite3; c=sqlite3.connect("/data/beehive.db"); print(c.execute("SELECT COALESCE(MAX(id), 0) FROM items").fetchone()[0])'
)"
RUN_ID="conclusion-first-v1"

podman exec -it beehive-research python -m scripts.run_collector \
  --mode rewrite-unread-summaries --db-path /data/beehive.db \
  --high-water-item-id "$HIGH_WATER_ITEM_ID" --run-id "$RUN_ID" --dry-run

podman exec -it beehive-research python -m scripts.run_collector \
  --mode rewrite-unread-summaries --db-path /data/beehive.db \
  --high-water-item-id "$HIGH_WATER_ITEM_ID" --run-id "$RUN_ID" \
  --canary-limit 10 --confirm-rewrite

podman exec -it beehive-research python -m scripts.run_collector \
  --mode rewrite-unread-summaries --db-path /data/beehive.db \
  --high-water-item-id "$HIGH_WATER_ITEM_ID" --run-id "$RUN_ID" --confirm-rewrite
```

The run is resumable and only updates items that are still unread. It prints progress as JSON and
exits nonzero if any item fails, so rerunning the same command safely retries remaining candidates.
To restore summaries changed by that run:

```bash
podman exec -it beehive-research python -m scripts.run_collector \
  --mode rollback-unread-summaries --db-path /data/beehive.db \
  --run-id "$RUN_ID" --confirm-rollback
```

On a local install, run the same commands with `.venv/bin/python` and your own `DB_PATH` instead of
`podman exec -it beehive-research python` and `/data/beehive.db`, with `COPILOT_GITHUB_TOKEN` set.

Rollback only restores a summary when that run's replacement is still live. If a later run or
manual edit changed it, the rollback exits nonzero and retains the log entry so it can be retried
after the later change is removed.

## Dashboard lifecycle contract

The project-owned declaration is `../ops/dashboard/workload.declaration.json`. It names logical
roles only: `web`, `research-worker` and `jobs-worker`, under lifecycle version 1, because
nothing is a wait-only one-shot any more. Concrete unit names, the host port, and the authority to
act on any of it stay in the dashboard repository's trusted binding, which also owns the
persistent pause gate. Nothing here grants itself permission; this file only describes what
exists.

The two Civic timers are not in the declaration yet, so a dashboard pause does not stop them.
Stop `beehive-civic.timer` and `beehive-civic-maintenance.timer` by hand if a pause must cover
Civic too.

Readiness is `/readyz` on the container's port 8000 (`GET`, expect `200`, 5s budget). It opens the
SQLite database through the app's own connection seam, checks the core tables are present, and
reads one row back, so a mounted-but-empty volume answers 503 instead of passing.

**Pause** stops `beehive-web.service` first, so nothing new is queued, then both workers. Each
worker drains itself on SIGTERM: it stops taking work, gives running jobs 30 seconds, and hands
back whatever claim is still held, within its `TimeoutStopSec=60`.

**Resume** goes back the other way: both workers, then `beehive-web.service`.

Nothing queued is lost across a pause. Every piece of work is a committed SQLite row (a "Fetch
now" request, a pending `deep_reads` job, a `research_runs`/`research_chat_requests` entry, a
Channel's due-time, an Email Group's watermark), not in-memory state. On resume each worker picks
its queues back up and reconciles any lease that expired while it was down.

Expect a burst right after resume: the jobs worker runs one fetch sweep, one digest check and one
reminder check as it starts, rather than waiting for the next quarter hour. Each re-derives what
is actually due (per-Channel schedules for fetch, watermarks for digest, the closing window for
reminders), so the catch-up costs one cycle rather than a backlog replay.

## Secrets (never in the image or git)

Beehive reads its credentials from rootless Podman secrets, each mapped to an environment
variable inside the container. Create them once on the host with your own values:

```bash
# Admin session cookie signing key (any high-entropy random value):
openssl rand -hex 32 | podman secret create beehive-session-secret -

# GitHub token for the Copilot-backed AI ranking call:
printf '%s' "$COPILOT_GITHUB_TOKEN" | podman secret create beehive-copilot-github-token -

# Azure Communication Services connection string for outbound email. If you use ACS, fetch the
# connection string for your own resource, e.g.:
az communication list-key --name <your-acs-resource> -g <your-resource-group> \
  --query primaryConnectionString -o tsv | podman secret create beehive-acs-connection -
```

- `beehive-session-secret` → `SESSION_SECRET` (admin session cookie signing, ADR-0005). The web
  app **refuses to start** without it, or with a value under 32 characters. An empty secret is not
  a degraded mode: `sign_session_id` HMACs with an empty key, so anyone could mint a valid-looking
  admin cookie.
- `beehive-copilot-github-token` → `COPILOT_GITHUB_TOKEN` (the jobs worker's AI ranking and
  deep-read briefs, and the Research worker's plan/sufficiency/synthesis/chat AI calls and LLM
  model-list refresh, plus the daily Civic generation and verification calls, all via
  `ai/llm_client.py`). The web and Civic maintenance containers do not receive it.
- `beehive-acs-connection` → `ACS_CONNECTION_STRING` (Email Group digests, Research-completion
  emails, Tracker reminders and failure alerts from the jobs worker, paired with the
  `DIGEST_EMAIL_TO`/`DIGEST_EMAIL_FROM` `Environment=` values on that container). Omit this secret
  to log delivery instead. The **web** container needs it too: the admin UI's Email Group "Test
  send" goes through the same notifier.

## Reverse proxy and client addresses

The login rate limiter locks out a source address after five failed attempts. It reads the source
from the TCP peer by default, and only honours a `CF-Connecting-IP` or `X-Forwarded-For` header
when the peer is listed in `TRUSTED_PROXY_IPS` (comma-separated addresses or CIDR blocks). Without
that, any client could send its own header and get a fresh five-attempt budget per request.

Set it to the address your proxy's traffic arrives from **as seen inside the container**, which is
the container network gateway, not the proxy's public address:

```bash
podman inspect beehive-web --format '{{.NetworkSettings.Gateway}}'
```

Leaving it unset is safe; it just means every remote client is attributed to that one gateway
address, so the per-IP limit behaves like a global one.

No Reddit credential is needed: the jobs worker's Reddit connector reads Reddit's public,
unauthenticated Atom RSS feed (`https://www.reddit.com/r/<subreddit>/hot/.rss`), not the OAuth
Data API — see `src/beehive/connectors/reddit.py`'s module docstring.

## One-time admin password bootstrap

Never stored in git, never in an env var or Podman secret — hashed with Argon2id straight into
the app's own SQLite DB. Re-run the same command later to rotate it; no redeploy needed:

```bash
podman exec -it beehive-web python -m scripts.set_admin_password --db-path /data/beehive.db
```

## Install / update the Quadlet units

`.container`/`.volume` files are Quadlet units (Podman's generator turns them into systemd
services) and belong in `~/.config/containers/systemd/`:

```bash
cp deploy/quadlet/beehive-data.volume deploy/quadlet/beehive-*.container ~/.config/containers/systemd/
systemctl --user daemon-reload
# Quadlet-generated units are auto-wanted by their [Install] section the moment the generator
# runs them at daemon-reload -- `systemctl --user enable` on one of these fails with "Unit ... is
# transient or generated", so only `start` is needed, and the generator re-creates the want
# automatically on every future boot.
systemctl --user start beehive-web.service beehive-research.service beehive-jobs.service
```

## Email schedule operations

The jobs worker checks Email Groups every 15 minutes. Each Email Group independently uses either a
fixed interval or selected weekdays plus a local time and IANA timezone. The check records every
send and delivery error. It also sends pending Research completion notifications to the
configured default recipient before evaluating Email Groups.

The Owner can inspect next-due times and delivery failures in Admin, see a first-send forecast
before saving, preview current pending content as HTML, and send a test copy without consuming
events or advancing the delivery checkpoint. An empty test sends a diagnostic message; log-only
transport is not reported as a successful email delivery. Selection and diagnostics both apply
the current channel score cutoff. Lowering a cutoff does not resurrect suppressed deliveries.
System Health summarizes missing recipients and recent delivery errors.

```bash
journalctl --user -u beehive-jobs.service -n 200 --no-pager | grep -i -e digest -e email
```

## Jobs worker (ADR-0012)

`beehive-jobs.container` runs `scripts/run_jobs.py`, one process with six lanes. Each lane runs
one job at a time on its own thread:

| Lane | What it runs | When |
|------|--------------|------|
| Fetch now | The Channels the admin queued, forced past their schedules | Within 5 seconds of the request |
| Scheduled fetching | Every Channel in turn; each Source's schedule decides whether it is due | At start-up and every quarter hour |
| Organization rules | Draft previews and explicit reprocessing of eligible existing items | On each worker poll, with previews first |
| Deep reads | The oldest queued article brief | Within 5 seconds of the request |
| Email | Research-completion emails and due Email Group digests | At start-up and every 15 minutes |
| Reminders | Due Tracker reminders | At start-up and every 5 minutes |

The two fetch lanes and rule lane never work on the same Channel at once. Draft Channels wait for
explicit activation. Applies yield after each batch without using failure retries, rotate fairly,
and allow their Channel's waiting scheduled fetch between batches. Other Channels continue across
sweep boundaries even while an apply is running.

Only one process runs jobs at a time: the worker holds a file lock (`flock`) on
`beehive.db.jobs.lock` beside the database until it exits, and a second worker started against the
same database waits until the first stops. A job that fails is logged and its lane moves on; the
Channel's Sources show the error in the admin. A job that runs past its lane's limit (60 minutes
for a fetch or rule batch, 30 for a deep read or an email pass, 15 for reminders) makes the worker
hand back its claims and exit, and systemd restarts it. The stuck
job's own claim counts as a failed try: a "Fetch now" request is dropped after three tries, and
a deep read is marked failed so it can be retried from its page. The admin's **System** chapter
shows when the worker last checked in, what is waiting, and any lane that has been busy for too
long.

Rule requests keep progress and leased claims in SQLite. Recovery retries interrupted work;
cancellation retains results already applied. Profile and content checks prevent stale updates,
and reprocessing does not create discovery events or resend delivered notifications. A healthy
apply can span many batches without a total elapsed-time cap.

A Channel whose AI ranking keeps failing sends its **Failure alert email** at most once every six
hours, however many cycles fail in between.

```bash
systemctl --user status beehive-jobs.service
journalctl --user -u beehive-jobs.service -n 200 --no-pager
```

After fixing a Source, select **Fetch now** in the admin; the worker runs it within seconds. The
`scripts.run_collector` job modes refuse to run while the worker is active, so a Channel is never
fetched by two processes at once.

## Civic (PartyMap) worker (ADR-0013)

Install the units and enable both timers once the image and intake directory below exist:

```bash
cp deploy/quadlet/beehive-civic*.container ~/.config/containers/systemd/
# Timers are plain systemd units, not Quadlet files.
install -m 0644 deploy/quadlet/beehive-civic*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now beehive-civic.timer beehive-civic-maintenance.timer
```

`beehive-civic.timer` starts the daily job at `06:30` in `Pacific/Auckland`. The global Watch
resolves its immutable calendar slot through the application scheduler, so a persistent timer
catch-up cannot create the same slot twice. PartyMap mode requires exactly one active Watch with
subject `partymap`, the same 06:30 Auckland schedule, and a published vocabulary; a
party-per-Watch layout fails before enqueueing.

The repository Civic Quadlet templates use `Image=localhost/beehive:civic`, not the shared
`:latest` image. Build the dedicated image from the repository root before installing the Civic
units or enabling their timers:

```bash
podman build -t localhost/beehive:civic -f deploy/Containerfile.civic .
```

This recipe pins the Python and uv image digests and installs dependencies from `uv.lock` with
`uv sync --locked --no-dev --extra ai --extra email --no-editable`.

Treat `:civic` as the local staging tag. For an integrated rollout, assign the built image a
unique release tag and set `Image=` in both installed Civic units to that same tag before
reloading or restarting them. Never reuse a release tag. Retain the previous release image for
rollback. Once the replacement is healthy, remove superseded stopped containers and older images,
keeping the current image and one rollback image. Do not prune named volumes, database data, or
spool artifacts as image cleanup.

Receipt revisions require both Civic jobs to move to revision-aware code together. After new
revisions have been written, do not point an older revision-unaware image at the migrated
database. Preserve the pre-rollout database/spool snapshot; after new publications are accepted,
prefer a compatible rollback image or a forward fix rather than restoring older publication
state. See [Civic publication reliability](../docs/civic-publication-reliability.md).

Use the web container's existing database mount to plan and then explicitly apply that Owner
configuration. Neither command starts a timer or needs an AI credential:

```bash
podman exec beehive-web python -m scripts.setup_partymap_civic
podman exec beehive-web python -m scripts.setup_partymap_civic --apply
```

The apply is atomic and idempotent. It refuses any conflicting active Watch, incompatible
`partymap` schedule/status, Owner draft, or non-matching published Vocabulary instead of
superseding Owner configuration.

Create the rootless host seam before starting either timer:

```bash
install -d -m 0700 \
  "$HOME/.local/share/partymap-intake/inbox" \
  "$HOME/.local/share/partymap-intake/outcomes"
```

Both Civic containers bind `%h/.local/share/partymap-intake` at `/spool` with Podman's shared
SELinux relabel option (`:z`) and set `CIVIC_SPOOL_ROOT=/spool`. The SQLite database remains in
`beehive-data.volume`; it is not exposed through the seam. The host PartyMap Publisher runs as
the same rootless user and reads/writes the host directory directly. Keep it mode `0700` (files
are written `0600`). On SELinux hosts, retain `:z` on both mounts so the two containers share one
label; do not separately relabel or chown the directory to a container-only UID.

Inbox existence alone never records delivery. `beehive-civic-maintenance.timer` first recovers
expired Run/Batch/Notification leases, replays any missing or identical inbox file, then validates
outcomes. Each projection pass writes every deliverable Batch without a regular inbox file (at most
50, continuing after a persisted cursor) and byte-verifies the next 20 existing files after a
second cursor, so an unsettled shadow backlog never starves newer Batches. Both cursors advance by
compare-and-set in `app_state`. Accepted outcomes consume one delivery claim and receipt the whole
Batch. Rejected outcomes quarantine it. Missing outcomes leave it sealed with `attempt_count`
unchanged.
An accepted outcome with no proposal is delivered only when the inbox envelope is empty; for a
non-empty envelope that local-apply acknowledgement is shadow-only and production quarantines it.
After either durable settlement, maintenance atomically moves the envelope to
`processed/accepted` or `processed/rejected` and fsyncs both sides of the rename. The outcome JSON
stays in `outcomes`. A crash after DB settlement or during the move is replayed on the next tick;
shadow mode leaves every envelope in `inbox`.

For a shadow rollout, set `Environment=CIVIC_SPOOL_SHADOW=1` on both Civic containers. The daily
job still writes exact v1 envelopes and maintenance still validates outcome files, but accepted
outcomes leave the Batch sealed and write no receipt.

```bash
systemctl --user status beehive-civic.timer beehive-civic-maintenance.timer
journalctl --user -u beehive-civic.service -o cat --since "48 hours ago" --no-pager
journalctl --user -u beehive-civic-maintenance.service -o cat \
  --since "48 hours ago" --no-pager
```

Both services use unbuffered Python output and write immediately flushed
`partymap.civic.event.v1` NDJSON to journald under the identifiers `beehive-civic` and
`beehive-civic-maintenance`. One `correlation_id` is generated per command invocation and shared
by Worker, spool, outcome, and recovery events. systemd limits each service to 1,000 messages per
30 seconds. The existing final JSON summaries are unchanged in shape and remain at the end of
each command run.

For a read-only health snapshot, run the image without network access or credentials and mount
both stores read-only:

```bash
podman run --rm --network=none --read-only \
  -v beehive-data:/data:ro \
  -v "$HOME/.local/share/partymap-intake:/spool:ro,z" \
  localhost/beehive:latest \
  -m scripts.diagnose_partymap_civic \
  --db-path /data/beehive.db --spool-root /spool --since-hours 48 --pretty
```

The report contains Watch schedules, vocabulary term counts, recent lifecycle counts and
timestamps, hashes, idempotency keys, bounded provider references, spool file metadata, and
anomaly codes. It never includes article text, titles, statements, URLs, prompts, model output,
notification bodies, environment values, or database paths. Prefer this report over sharing the
SQLite database, inbox/outcome files, or broad process logs. IDs, hashes, filenames, and provider
references can still identify internal work, so review the JSON before sending it outside the
operations team.

## Research worker (ADR-0009)

`beehive-research.container` is the one durable process for both Research Runs and Research Chat
replies: two independent, database-enforced bounded pools (3 concurrent Research Runs, 3
concurrent chat replies by default) so a handful of long research runs can never starve a chat
reply. It polls `research_runs`/`research_chat_requests` directly and reconciles expired leases
itself on startup and every minute while running, so it needs no separate backstop timer.

The worker also keeps the admin's LLM model list current, because it is the only always-on
process with the Copilot token. The "Refresh list" button in Global settings stores a request in
`app_state`; the worker picks it up on its next poll, asks the SDK which models the account
offers, and stores the list for the settings page (`ai/model_catalog.py`). It also refreshes on
its own once a day. If a request shows as not started, check that this unit is running.

### Environment overrides

The worker's product ceilings (the 3-processing-Research-Run cap enforced by
`db/research_runs.py`, and each run's fixed 20-minute deadline) are never overridable — only its
own operational knobs are, via environment variables on `beehive-research.container`, e.g.:

```
Environment=RESEARCH_WORKER_RESEARCH_POOL_SIZE=3
Environment=RESEARCH_WORKER_CHAT_POOL_SIZE=3
Environment=RESEARCH_WORKER_POLL_INTERVAL_SECONDS=5
Environment=RESEARCH_WORKER_LEASE_SECONDS=90
Environment=RESEARCH_WORKER_HEARTBEAT_INTERVAL_SECONDS=30
Environment=RESEARCH_WORKER_RECONCILE_INTERVAL_SECONDS=60
Environment=RESEARCH_WORKER_SHUTDOWN_GRACE_SECONDS=30
```

An invalid value (non-numeric, non-positive, or a heartbeat interval that is not smaller than the
lease it is meant to renew) makes `scripts/run_research_worker.py` exit nonzero immediately at
startup instead of running with a broken configuration.

### Diagnostics

```bash
# Is the always-on worker running, and what has it logged recently?
systemctl --user status beehive-research.service
journalctl --user -u beehive-research.service -n 200 --no-pager

# Run one reconcile sweep by hand (safe at any time — idempotent, recovers only expired leases):
podman exec -it beehive-research python -m scripts.run_research_worker --reconcile-once
```

### Rollout / rollback

```bash
# Rebuild the shared image, then restart the worker to pick it up:
podman build -t localhost/beehive:latest -f Containerfile .
systemctl --user restart beehive-research.service

# Roll back to a previously tagged image if a rollout misbehaves:
podman tag localhost/beehive:<previous-tag> localhost/beehive:latest
systemctl --user restart beehive-research.service
```

`beehive-research.service`'s `TimeoutStopSec=60` gives the worker's own graceful shutdown (default
30s grace, see `RESEARCH_WORKER_SHUTDOWN_GRACE_SECONDS` above) room to finish before systemd sends
SIGKILL. After the grace period, in-flight Research Runs and chat replies are requeued without
setting the Owner cancellation flag. A replacement worker resumes the existing staged snapshot,
and stale worker writes remain claim-fenced. Any claim that still does not resolve before a hard
kill is recovered by the worker's next reconcile sweep once its lease expires.
