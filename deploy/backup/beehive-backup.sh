#!/usr/bin/env bash
# Nightly WAL-safe online backup of the Beehive SQLite database, with a restore drill on every
# run. The archive is written under a hidden .partial name, restored into a scratch file and
# verified, flushed, and only then renamed into place, so every file matching the retention glob
# is a complete archive that is known to open.
#
# The online backup API copies a consistent snapshot while the collector, digest, and web
# containers keep writing, so no container has to stop. Runs on the host with the sqlite3 CLI;
# rootless Podman stores the volume under the owner's home directory.
#
# Override via env: BEEHIVE_DB_PATH, BEEHIVE_BACKUP_DIR, BEEHIVE_BACKUP_RETENTION_DAYS,
#                   BEEHIVE_BACKUP_KEEP_MIN, BEEHIVE_BACKUP_VERIFY_SCRIPT.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB="${BEEHIVE_DB_PATH:-$HOME/.local/share/containers/storage/volumes/beehive-data/_data/beehive.db}"
BACKUP_DIR="${BEEHIVE_BACKUP_DIR:-$HOME/backups/beehive}"
RETENTION_DAYS="${BEEHIVE_BACKUP_RETENTION_DAYS:-14}"
KEEP_MIN="${BEEHIVE_BACKUP_KEEP_MIN:-3}"
VERIFY="${BEEHIVE_BACKUP_VERIFY_SCRIPT:-$SCRIPT_DIR/verify-beehive-backup.py}"

if [[ ! -f "$DB" ]]; then
  echo "beehive-backup: database not found: $DB" >&2
  exit 1
fi

umask 077
mkdir -p "$BACKUP_DIR"
chmod 0700 "$BACKUP_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
FINAL="$BACKUP_DIR/beehive-${STAMP}.db.gz"
SNAPSHOT="$BACKUP_DIR/.beehive-${STAMP}-$$.db.partial"
PARTIAL="$BACKUP_DIR/.beehive-${STAMP}-$$.db.gz.partial"

cleanup() {
  rm -f "$SNAPSHOT" "$PARTIAL"
}
trap cleanup EXIT

live_items() {
  sqlite3 "file:${DB}?mode=ro" ".timeout 10000" "SELECT COUNT(*) FROM items;"
}

ITEMS_BEFORE="$(live_items)"
sqlite3 "file:${DB}?mode=ro" ".timeout 10000" ".backup '${SNAPSHOT}'"
ITEMS_AFTER="$(live_items)"
gzip -c "$SNAPSHOT" > "$PARTIAL"
rm -f "$SNAPSHOT"

# A point-in-time copy must hold between the live counts taken just before and just after it.
if (( ITEMS_BEFORE <= ITEMS_AFTER )); then
  LOW="$ITEMS_BEFORE"; HIGH="$ITEMS_AFTER"
else
  LOW="$ITEMS_AFTER"; HIGH="$ITEMS_BEFORE"
fi
python3 "$VERIFY" "$PARTIAL" --items-between "$LOW" "$HIGH"

sync "$PARTIAL"
mv "$PARTIAL" "$FINAL"

# Retention: drop archives older than RETENTION_DAYS, but always keep the newest KEEP_MIN.
mapfile -t ARCHIVES < <(ls -1t "$BACKUP_DIR"/beehive-*.db.gz 2>/dev/null || true)
for index in "${!ARCHIVES[@]}"; do
  (( index < KEEP_MIN )) && continue
  if [[ -n "$(find "${ARCHIVES[$index]}" -mtime +"$RETENTION_DAYS" -print)" ]]; then
    rm -f "${ARCHIVES[$index]}"
  fi
done

echo "beehive-backup: wrote $FINAL ($(du -h "$FINAL" | cut -f1))"
