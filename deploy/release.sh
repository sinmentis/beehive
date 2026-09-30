#!/usr/bin/env bash
# Release Beehive from a clean commit, in the order the schema versioning needs:
#
#   deploy/release.sh build            build localhost/beehive:<sha> from HEAD (clean tree only)
#   deploy/release.sh migrate <tag>    back up, then run that image's `--mode migrate` once
#   deploy/release.sh promote <tag>    keep the old image as :rollback, point :latest at <tag>,
#                                      restart the always-on units, wait for /readyz, and check
#                                      that every unit stays up
#   deploy/release.sh prune            drop SHA-tagged images other than :latest and :rollback
#
# Every unit is always on (the web app and two workers, ADR-0012), so promote restarts them all.
# Roll back with `deploy/release.sh promote rollback`, which swaps :latest and :rollback. That
# works across additive migrations because older code accepts a newer, compatible schema (see
# src/beehive/db/connection.py). Rolling back past a release that changed the unit files also
# needs the old unit files; deploy/README.md says how.
#
# Override via env: BEEHIVE_IMAGE, BEEHIVE_VOLUME, BEEHIVE_READYZ_URL, BEEHIVE_SKIP_BACKUP=1,
# BEEHIVE_SETTLE_SECONDS. BEEHIVE_PODMAN, BEEHIVE_SYSTEMCTL and BEEHIVE_CURL exist for the test
# harness only.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${BEEHIVE_IMAGE:-localhost/beehive}"
VOLUME="${BEEHIVE_VOLUME:-beehive-data}"
READYZ_URL="${BEEHIVE_READYZ_URL:-http://127.0.0.1:8095/readyz}"
PODMAN="${BEEHIVE_PODMAN:-podman}"
SYSTEMCTL="${BEEHIVE_SYSTEMCTL:-systemctl}"
CURL="${BEEHIVE_CURL:-curl}"
ALWAYS_ON_UNITS=(beehive-research.service beehive-jobs.service beehive-web.service)
SETTLE_SECONDS="${BEEHIVE_SETTLE_SECONDS:-10}"

die() {
  echo "release: $*" >&2
  exit 1
}

image_id() {
  "$PODMAN" image inspect "$1" --format '{{.Id}}'
}

require_tag() {
  [[ -n "${1:-}" ]] || die "missing image tag"
  "$PODMAN" image exists "$IMAGE:$1" || die "no image $IMAGE:$1"
}

cmd_build() {
  cd "$REPO"
  [[ -z "$(git status --porcelain)" ]] || die "working tree is dirty; release only committed code"
  local sha short
  sha="$(git rev-parse HEAD)"
  short="$(git rev-parse --short=7 HEAD)"
  "$PODMAN" build --label "org.opencontainers.image.revision=$sha" -t "$IMAGE:$short" \
    -f Containerfile . >&2
  echo "$short"
}

cmd_migrate() {
  require_tag "${1:-}"
  if [[ "${BEEHIVE_SKIP_BACKUP:-0}" != "1" ]]; then
    "$SYSTEMCTL" --user start beehive-backup.service
  fi
  # No --log-driver passthrough here: Podman refuses it on a terminal, and this runs from one.
  "$PODMAN" run --rm -v "$VOLUME:/data" --env DB_PATH=/data/beehive.db \
    "$IMAGE:$1" -m scripts.run_collector --mode migrate
}

# /readyz only proves the web app. The workers serve no port, so a worker crash-looping on the new
# image would pass unnoticed: each unit must still be active, with no new restarts, a little later.
check_units_stay_up() {
  local unit restarts=()
  for unit in "${ALWAYS_ON_UNITS[@]}"; do
    restarts+=("$("$SYSTEMCTL" --user show -p NRestarts --value "$unit")")
  done
  sleep "$SETTLE_SECONDS"
  local i=0
  for unit in "${ALWAYS_ON_UNITS[@]}"; do
    "$SYSTEMCTL" --user is-active --quiet "$unit" \
      || die "$unit is not running on the new image; roll back with: $0 promote rollback"
    [[ "$("$SYSTEMCTL" --user show -p NRestarts --value "$unit")" == "${restarts[$i]}" ]] \
      || die "$unit restarted on the new image; roll back with: $0 promote rollback"
    i=$((i + 1))
  done
}

cmd_promote() {
  require_tag "${1:-}"
  local current target
  # Resolve the target before moving any tag, so `promote rollback` really swaps the two images
  # instead of re-promoting :latest after :rollback has been pointed at it.
  target="$(image_id "$IMAGE:$1")"
  current="$(image_id "$IMAGE:latest" 2>/dev/null || true)"
  if [[ -n "$current" && "$current" != "$target" ]]; then
    "$PODMAN" tag "$current" "$IMAGE:rollback"
  fi
  "$PODMAN" tag "$target" "$IMAGE:latest"
  "$SYSTEMCTL" --user restart "${ALWAYS_ON_UNITS[@]}"
  for _ in $(seq 1 60); do
    if "$CURL" -fsS "$READYZ_URL" >/dev/null 2>&1; then
      check_units_stay_up
      echo "release: $IMAGE:$1 is live and ready"
      return 0
    fi
    sleep 1
  done
  die "$READYZ_URL did not become ready; roll back with: $0 promote rollback"
}

cmd_prune() {
  local keep=() removed=() ref id tag
  for tag in latest rollback; do
    if "$PODMAN" image exists "$IMAGE:$tag"; then
      keep+=("$(image_id "$IMAGE:$tag")")
    fi
  done
  while read -r ref id; do
    [[ "${ref##*:}" =~ ^[0-9a-f]{7}$ ]] || continue
    [[ " ${keep[*]} " == *" $id "* ]] && continue
    "$PODMAN" untag "$ref"
    removed+=("$id")
  done < <("$PODMAN" images "$IMAGE" --format '{{.Repository}}:{{.Tag}} {{.ID}}' --no-trunc \
    | sed 's/ sha256:/ /')
  # Delete only the Beehive images just untagged, and only once no other tag or container still
  # uses them. A host-wide `podman image prune` would also reach other projects' images.
  for id in "${removed[@]}"; do
    "$PODMAN" image rm "$id" >/dev/null 2>&1 || true
  done
}

case "${1:-}" in
  build) cmd_build ;;
  migrate) cmd_migrate "${2:-}" ;;
  promote) cmd_promote "${2:-}" ;;
  prune) cmd_prune ;;
  *) die "usage: $0 build | migrate <tag> | promote <tag> | prune" ;;
esac
