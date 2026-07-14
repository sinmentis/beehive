# Beehive deployment (rootless Podman + Quadlet)

Beehive runs on a single rootless-Podman host as one shared image (`../Containerfile`) that
serves three roles, each selected by a Quadlet unit's `Exec=`:

- an always-on public web app (Dashboard, Channel drill-down, and `/admin/*`),
- a timer-triggered fetch/AI-rank cycle, and
- a once-daily digest email job.

The public read surfaces are served directly; `/admin/*` and all write actions are gated by the
app's own password login (ADR-0003, ADR-0005), so no host-level identity gateway is required. If
you want to expose Beehive beyond localhost, put any reverse proxy or tunnel (nginx, Caddy,
cloudflared, etc.) in front of the web container's published port. The examples below use
placeholder values; replace them with your own.

## Files

| Path | Purpose |
|------|---------|
| `../Containerfile` | Single shared image for all three roles; `ENTRYPOINT` is bare `python`, each unit supplies its own `-m scripts...` invocation |
| `quadlet/beehive-data.volume` | Named Podman volume backing `/data` (the SQLite DB), shared by all four units below |
| `quadlet/beehive-web.container` | Always-on web app — `PublishPort=127.0.0.1:8095:8000`, `Restart=always` |
| `quadlet/beehive-fetch.container` + `.timer` | Fetch → dedup → AI-rank cycle, every 3 hours |
| `quadlet/beehive-fetch-manual.container` + `.path` | Manual per-Channel trigger — started only when the admin UI writes a trigger marker, never on a timer |
| `quadlet/beehive-digest.container` + `.timer` | Once-daily digest email, 08:00 Pacific/Auckland |

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
  via `ai/llm_client.py`).
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
```
