#!/usr/bin/env bash
# Snapshot beta users and project data. Safe to run while the backend is live.
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
python="$repo/.venv/bin/python"
dest="${WIREUP_BACKUP_DIR:-$HOME/wireup-backups}"
stamp=$(date +%Y%m%d-%H%M%S)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$dest"
if [ -f "$repo/backend/data/beta.sqlite3" ]; then
  "$python" - "$repo/backend/data/beta.sqlite3" "$tmp/beta.sqlite3" <<'PY'
import sqlite3, sys
source = sqlite3.connect(f'file:{sys.argv[1]}?mode=ro', uri=True)
target = sqlite3.connect(sys.argv[2])
with target:
    source.backup(target)
source.close(); target.close()
PY
fi
tar -czf "$dest/wireup-$stamp.tar.gz" -C "$repo/backend" data
find "$dest" -name 'wireup-*.tar.gz' -mtime +14 -delete
echo "$dest/wireup-$stamp.tar.gz"
