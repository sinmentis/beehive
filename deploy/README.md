# Beehive deployment (rootless Podman + Quadlet)

Beehive runs on a single rootless-Podman host as one shared image (`../Containerfile`). Each
process selects its role through a Quadlet unit's `Exec=`:

- an always-on public web app (Dashboard, Channel drill-down, and `/admin/*`),
- a timer-triggered fetch/AI-rank cycle,
- a 15-minute Email Group schedule and Research-completion notification job,
- a five-minute Tracker reminder job, currently backed by the auction adapter,
- a queued, owner-triggered article deep-read worker, and
- an always-on durable Research worker (Research Runs + Research Chat replies, ADR-0009), backed
  by a periodic reconciliation timer.

The public read surfaces are served directly; `/admin/*` and all write actions are gated by the
app's own password login (ADR-0003, ADR-0005), so no host-level identity gateway is required. If
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
| `quadlet/beehive-fetch.container` + `.timer` | Fetch → dedup → AI-rank cycle; the timer wakes every 15 minutes and each Channel's own interval or daily calendar schedule decides whether it runs |
| `quadlet/beehive-fetch-manual.container` + `.path` | Manual per-Channel trigger — started only when the admin UI writes a trigger marker, never on a timer |
| `quadlet/beehive-digest.container` + `.timer` | Evaluates Email Group interval/calendar schedules and pending Research-completion emails every 15 minutes |
| `quadlet/beehive-auction-reminders.container` + `.timer` | Runs the generic Tracker reminder worker every 5 minutes; the current auction adapter claims watched lots inside the one-hour closing window |
| `quadlet/beehive-deep-read.container` + `.path` + `.timer` | Bounded article brief worker; the path provides low-latency wakeup and the timer reconciles missed wakeups |
| `quadlet/beehive-research.container` | Always-on durable Research worker — bounded Research Run + Research Chat pools (ADR-0009), `Restart=always` |
| `quadlet/beehive-research-reconcile.container` + `.timer` | Oneshot expired-lease recovery sweep, hourly — backstops the always-on worker (which reconciles every minute itself) after a crash/restart; claims/executes nothing |
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
through `podman events`. Read a job's output with `journalctl --user -u beehive-fetch.service`;
`podman logs` does not apply to passthrough containers.

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

1. Pause Beehive (the dashboard's pause, or stop the timers, path units, `beehive-web.service`
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
back is just:

```bash
podman tag localhost/beehive:rollback localhost/beehive:latest
systemctl --user restart beehive-research.service beehive-web.service
```

## Dashboard lifecycle contract

The project-owned declaration is `../ops/dashboard/workload.declaration.json`. It names logical
roles only — nine actionable ones (`web`, `research-worker`, and the five timers plus two path
units) and six wait-only drain roles (the one-shot jobs behind those timers and paths). Concrete
unit names, the host port, and the authority to act on any of it stay in the dashboard
repository's trusted binding, which also owns the persistent pause gate. Nothing here grants
itself permission; this file only describes what exists.

Readiness is `/readyz` on the container's port 8000 (`GET`, expect `200`, 5s budget). It opens the
SQLite database through the app's own connection seam, checks the core tables are present, and
reads one row back, so a mounted-but-empty volume answers 503 instead of passing.

**Pause** stops things in dependency order, newest work first:

1. the five timers (`fetch`, `digest`, `auction-reminders`, `deep-read`, `research-reconcile`), so
   no new scheduled run can begin;
2. the two path units (`fetch-manual`, `deep-read`), so a trigger marker written a second ago no
   longer starts a job;
3. `beehive-web.service`, which is what writes those markers and enqueues research work;
4. `beehive-research.service`, last, so it drains with its own graceful shutdown
   (`TimeoutStopSec=60`) after nothing upstream can hand it more work.

The six one-shot jobs are **wait-only**. The dashboard never sends them a stop. A fetch cycle, a
deep-read brief, a digest send or a reminder claim is mid-transaction against the shared SQLite
file, and killing one buys nothing that waiting does not. Pause reports success only once every
actionable unit is stopped *and* every in-flight one-shot has exited on its own.

**Resume** goes back the other way: `beehive-research.service`, then `beehive-web.service`, then
the two path units, then the timers.

Nothing queued is lost across a pause. Every trigger is a committed SQLite row (a pending
`deep_reads` job, a `research_runs`/`research_chat_requests` entry, a Channel's due-time, an Email
Group's watermark), not in-memory state, and the wakeup markers on disk outlive the pause too. On
resume the worker picks its queue back up and reconciles any lease that expired while it was down.

Expect a burst right after resume: all five timers are `Persistent=true`, so systemd runs one
catch-up pass for the ticks missed during the pause rather than silently skipping them. That is a
single run per timer, not one per missed interval, and each job re-derives what is actually due
(per-Channel schedules for fetch, watermarks for digest, the closing window for reminders), so the
catch-up costs one cycle rather than a backlog replay.

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
- `beehive-copilot-github-token` → `COPILOT_GITHUB_TOKEN` (the fetch container's AI ranking call,
  the deep-read container's article brief generation, and the always-on Research worker's plan/
  sufficiency/synthesis/chat AI calls, all via `ai/llm_client.py`). The web container and the
  Research reconcile-sweep container do not receive this secret — reconciliation only recovers
  expired leases, it never calls the AI.
- `beehive-acs-connection` → `ACS_CONNECTION_STRING` (Email Group, Research-completion, and
  Tracker-reminder delivery, paired with the `DIGEST_EMAIL_TO`/`DIGEST_EMAIL_FROM`
  `Environment=` values on those containers). Omit this secret to log delivery instead. The **web**
  container needs it too: the admin UI's Email Group "Test send" goes through the same notifier.

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

No Reddit credential is needed: the fetch container's Reddit connector reads Reddit's public,
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
services) and belong in `~/.config/containers/systemd/`. Plain `.timer`/`.path` files are NOT a
Quadlet unit type — Quadlet ignores them there — so they go straight into the standard systemd
user unit directory instead:

```bash
cp deploy/quadlet/beehive-data.volume deploy/quadlet/beehive-*.container ~/.config/containers/systemd/
cp deploy/quadlet/beehive-*.timer deploy/quadlet/beehive-*.path ~/.config/systemd/user/
systemctl --user daemon-reload
# Quadlet-generated units (.container/.volume) are auto-wanted by their [Install] section the
# moment the generator runs them at daemon-reload -- `systemctl --user enable` on one of these
# fails with "Unit ... is transient or generated", so only `start` is needed, and the generator
# re-creates the want automatically on every future boot.
systemctl --user start beehive-web.service
# Plain systemd units (.timer/.path) are NOT auto-wanted -- they need an explicit `enable` to
# persist across reboots, same as any regular unit file.
systemctl --user enable --now beehive-fetch.timer
systemctl --user enable --now beehive-digest.timer
systemctl --user enable --now beehive-auction-reminders.timer
systemctl --user enable --now beehive-fetch-manual.path
systemctl --user enable --now beehive-deep-read.path
systemctl --user enable --now beehive-deep-read.timer
systemctl --user start beehive-research.service
systemctl --user enable --now beehive-research-reconcile.timer
```

When the owner requests a brief, the web process commits a pending SQLite job before writing the
wakeup marker. The marker is only a latency hint: `beehive-deep-read.timer` starts the same bounded
worker every 30 minutes so queued work is not stranded if the path event is missed.

## Email schedule operations

`beehive-digest.timer` runs every 15 minutes. Each Email Group independently uses either a fixed
interval or selected weekdays plus a local time and IANA timezone. The job records every check,
successful send, and delivery error. It also sends pending Research completion notifications to
the configured default recipient before evaluating Email Groups.

The Owner can inspect next-due times and delivery failures in Admin, preview current pending
content, and send a test copy without consuming events. System Health summarizes missing
recipients and recent delivery errors.

```bash
systemctl --user status beehive-digest.timer
systemctl --user list-timers beehive-digest.timer --no-pager
journalctl --user -u beehive-digest.service -n 100 --no-pager
```

## Research worker (ADR-0009)

`beehive-research.container` is the one durable process for both Research Runs and Research Chat
replies: two independent, database-enforced bounded pools (3 concurrent Research Runs, 3
concurrent chat replies by default) so a handful of long research runs can never starve a chat
reply. It polls `research_runs`/`research_chat_requests` directly — no path/wakeup marker is
needed, unlike the deep-read worker — and reconciles expired leases itself on startup and
periodically while running. `beehive-research-reconcile.container` + `.timer` is a separate,
lightweight, oneshot backstop: it only recovers already-expired leases (idempotent, claims/
executes nothing) in case the always-on worker itself crashed or was mid-restart when a lease
expired. Because the worker already sweeps every minute, the backstop runs hourly.

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

# Did the last reconcile sweep run, and did it recover anything?
systemctl --user status beehive-research-reconcile.service
journalctl --user -u beehive-research-reconcile.service -n 50 --no-pager

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
kill is recovered by the next reconcile sweep once its lease expires.
