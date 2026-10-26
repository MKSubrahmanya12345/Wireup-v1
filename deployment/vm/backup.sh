#!/usr/bin/env bash
# Snapshot beta users and project data without stopping the backend.
# Installed to /usr/local/bin/wireup-backup by deployment/vm/bootstrap.sh and
# run nightly by wireup-backup.timer.
set -euo pipefail

APP_DIR=${WIREUP_APP_DIR:-/opt/wireup/app}
DEST=${WIREUP_BACKUP_DIR:-/var/backups/wireup}
KEEP_DAYS=${WIREUP_BACKUP_DAYS:-14}
stamp=$(date +%Y%m%d-%H%M%S)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$DEST"
db="$APP_DIR/backend/data/beta.sqlite3"
if [ -f "$db" ]; then
  python3 - "$db" "$tmp/beta.sqlite3" <<'PY'
import sqlite3, sys

source = sqlite3.connect(f'file:{sys.argv[1]}?mode=ro', uri=True)
target = sqlite3.connect(sys.argv[2])
with target:
    source.backup(target)
source.close()
target.close()
PY
fi
# beta.sqlite3 is a consistent snapshot; the live WAL/SHM belong to the running
# database and must not be restored alongside it.
tar -czf "$DEST/wireup-$stamp.tar.gz" \
  --exclude='data/beta.sqlite3-wal' \
  --exclude='data/beta.sqlite3-shm' \
  -C "$APP_DIR/backend" data
find "$DEST" -name 'wireup-*.tar.gz' -mtime +"$KEEP_DAYS" -delete
echo "$DEST/wireup-$stamp.tar.gz"
