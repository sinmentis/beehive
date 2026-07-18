# Beehive deployment (rootless Podman + Quadlet)

Beehive runs on a single rootless-Podman host as one shared image (`../Containerfile`). Each
process selects its role through a Quadlet unit's `Exec=`:

- an always-on public web app (Dashboard, Channel drill-down, and `/admin/*`),
- a timer-triggered fetch/AI-rank cycle, and
- a once-daily digest email job,
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
| `quadlet/beehive-fetch.container` + `.timer` | Fetch → dedup → AI-rank cycle, every 3 hours |
| `quadlet/beehive-fetch-manual.container` + `.path` | Manual per-Channel trigger — started only when the admin UI writes a trigger marker, never on a timer |
| `quadlet/beehive-digest.container` + `.timer` | Once-daily digest email, 08:00 Pacific/Auckland |
| `quadlet/beehive-deep-read.container` + `.path` + `.timer` | Bounded article brief worker; the path provides low-latency wakeup and the timer reconciles missed wakeups |
| `quadlet/beehive-research.container` | Always-on durable Research worker — bounded Research Run + Research Chat pools (ADR-0009), `Restart=always` |
| `quadlet/beehive-research-reconcile.container` + `.timer` | Oneshot expired-lease recovery sweep, every 5 minutes — backstops the always-on worker after a crash/restart; claims/executes nothing |

The web container publishes to `127.0.0.1` only, so the app is reachable from the host's loopback
and from whatever reverse proxy or tunnel you place in front of it, not from the public internet
directly.

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

- `beehive-session-secret` → `SESSION_SECRET` (admin session cookie signing, ADR-0005 — the web
  container cannot protect `/admin/*` or write actions meaningfully without it, though it does not
  crash outright; see `web/deps.py`'s `require_admin_session`).
- `beehive-copilot-github-token` → `COPILOT_GITHUB_TOKEN` (the fetch container's AI ranking call,
  the deep-read container's article brief generation, and the always-on Research worker's plan/
  sufficiency/synthesis/chat AI calls, all via `ai/llm_client.py`). The web container and the
  Research reconcile-sweep container do not receive this secret — reconciliation only recovers
  expired leases, it never calls the AI.
- `beehive-acs-connection` → `ACS_CONNECTION_STRING` (the digest container's alert/digest email
  delivery, paired with the `DIGEST_EMAIL_TO`/`DIGEST_EMAIL_FROM` `Environment=` values on
  `beehive-digest.container`). Omit this secret to skip email; the app falls back to logging.

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
systemctl --user enable --now beehive-fetch-manual.path
systemctl --user enable --now beehive-deep-read.path
systemctl --user enable --now beehive-deep-read.timer
systemctl --user start beehive-research.service
systemctl --user enable --now beehive-research-reconcile.timer
```

When the owner requests a brief, the web process commits a pending SQLite job before writing the
wakeup marker. The marker is only a latency hint: `beehive-deep-read.timer` starts the same bounded
worker every five minutes so queued work is not stranded if the path event is missed.

## Research worker (ADR-0009)

`beehive-research.container` is the one durable process for both Research Runs and Research Chat
replies: two independent, database-enforced bounded pools (3 concurrent Research Runs, 3
concurrent chat replies by default) so a handful of long research runs can never starve a chat
reply. It polls `research_runs`/`research_chat_requests` directly — no path/wakeup marker is
needed, unlike the deep-read worker — and reconciles expired leases itself on startup and
periodically while running. `beehive-research-reconcile.container` + `.timer` is a separate,
lightweight, oneshot backstop: it only recovers already-expired leases (idempotent, claims/
executes nothing) in case the always-on worker itself crashed or was mid-restart when a lease
expired.

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
