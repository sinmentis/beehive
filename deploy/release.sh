#!/usr/bin/env bash
# Release Beehive from a clean commit, in the order the schema versioning needs:
#
#   deploy/release.sh build            build localhost/beehive:<sha> from HEAD (clean tree only)
#   deploy/release.sh migrate <tag>    back up, then run that image's `--mode migrate` once
#   deploy/release.sh promote <tag>    keep the old image as :rollback, point :latest at <tag>,
#                                      restart the always-on units, and wait for /readyz
#   deploy/release.sh prune            drop SHA-tagged images other than :latest and :rollback
#
# Every other unit is a oneshot started from :latest, so it picks the new image up on its next
# run. Rollback: `podman tag localhost/beehive:rollback localhost/beehive:latest`, then restart
# beehive-web.service and beehive-research.service. That works across additive migrations
# because older code accepts a newer, compatible schema (see src/beehive/db/connection.py).
#
# Override via env: BEEHIVE_IMAGE, BEEHIVE_VOLUME, BEEHIVE_READYZ_URL, BEEHIVE_SKIP_BACKUP=1.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${BEEHIVE_IMAGE:-localhost/beehive}"
VOLUME="${BEEHIVE_VOLUME:-beehive-data}"
READYZ_URL="${BEEHIVE_READYZ_URL:-http://127.0.0.1:8095/readyz}"
ALWAYS_ON_UNITS=(beehive-research.service beehive-web.service)

die() {
  echo "release: $*" >&2
  exit 1
}

require_tag() {
  [[ -n "${1:-}" ]] || die "missing image tag"
  podman image exists "$IMAGE:$1" || die "no image $IMAGE:$1"
}

cmd_build() {
  cd "$REPO"
  [[ -z "$(git status --porcelain)" ]] || die "working tree is dirty; release only committed code"
  local sha short
  sha="$(git rev-parse HEAD)"
  short="$(git rev-parse --short=7 HEAD)"
  podman build --label "org.opencontainers.image.revision=$sha" -t "$IMAGE:$short" \
    -f Containerfile . >&2
  echo "$short"
}

cmd_migrate() {
  require_tag "${1:-}"
  if [[ "${BEEHIVE_SKIP_BACKUP:-0}" != "1" ]]; then
    systemctl --user start beehive-backup.service
  fi
  podman run --rm --log-driver passthrough -v "$VOLUME:/data" --env DB_PATH=/data/beehive.db \
    "$IMAGE:$1" -m scripts.run_collector --mode migrate
}

cmd_promote() {
  require_tag "${1:-}"
  local current target
  current="$(podman image inspect "$IMAGE:latest" --format '{{.Id}}' 2>/dev/null || true)"
  target="$(podman image inspect "$IMAGE:$1" --format '{{.Id}}')"
  if [[ -n "$current" && "$current" != "$target" ]]; then
    podman tag "$current" "$IMAGE:rollback"
  fi
  podman tag "$IMAGE:$1" "$IMAGE:latest"
  systemctl --user restart "${ALWAYS_ON_UNITS[@]}"
  for _ in $(seq 1 60); do
    if curl -fsS "$READYZ_URL" >/dev/null 2>&1; then
      echo "release: $IMAGE:$1 is live and ready"
      return 0
    fi
    sleep 1
  done
  die "$READYZ_URL did not become ready; roll back with: podman tag $IMAGE:rollback $IMAGE:latest"
}

cmd_prune() {
  local keep=()
  for tag in latest rollback; do
    if podman image exists "$IMAGE:$tag"; then
      keep+=("$(podman image inspect "$IMAGE:$tag" --format '{{.Id}}')")
    fi
  done
  local ref id
  while read -r ref id; do
    [[ "${ref##*:}" =~ ^[0-9a-f]{7}$ ]] || continue
    if [[ ! " ${keep[*]} " =~ \ ${id}\  ]]; then
      podman untag "$ref"
    fi
  done < <(podman images "$IMAGE" --format '{{.Repository}}:{{.Tag}} {{.ID}}' --no-trunc \
    | sed 's/ sha256:/ /')
  podman image prune -f >/dev/null
}

case "${1:-}" in
  build) cmd_build ;;
  migrate) cmd_migrate "${2:-}" ;;
  promote) cmd_promote "${2:-}" ;;
  prune) cmd_prune ;;
  *) die "usage: $0 build | migrate <tag> | promote <tag> | prune" ;;
esac
