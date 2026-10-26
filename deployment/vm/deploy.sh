#!/usr/bin/env bash
# Ship the working tree to a cloud VM and (re)build/restart WireUp there.
#
#   deployment/vm/deploy.sh ubuntu@<vm-ip>
#   WIREUP_DOMAIN=wireup.space deployment/vm/deploy.sh ubuntu@<vm-ip>
#
# The first run installs everything (packages, Arduino cores, venv, Caddy
# certificate). Later runs are delta-sync + rebuild + restart, usually under a
# minute once the Arduino cores are present.
#
# Application data (backend/data/) is deliberately not uploaded or deleted:
# testers' projects and beta accounts live only on the VM.
set -euo pipefail

TARGET=${1:-}
APP_DIR=${2:-/opt/wireup/app}
DOMAIN=${WIREUP_DOMAIN:-wireup.space}

if [ -z "$TARGET" ]; then
  echo "usage: $0 <user@host> [app-dir]" >&2
  exit 2
fi

repo=$(cd "$(dirname "$0")/../.." && pwd)
cd "$repo"

echo "==> preparing $TARGET:$APP_DIR"
ssh -t "$TARGET" "sudo mkdir -p '$APP_DIR'"

echo "==> syncing $repo to $TARGET:$APP_DIR (delta only)"
rsync -az --delete --human-readable --rsync-path="sudo rsync" \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='node_modules/' \
  --exclude='dist/' \
  --exclude='backend/data/' \
  --exclude='backend/builds/' \
  --exclude='test-results/' \
  --exclude='playwright-report/' \
  --exclude='audit-artifacts/' \
  --exclude='tools/' \
  --exclude='vendor/qemu/qemu/' \
  --exclude='vendor/qemu/*.tar.xz' \
  --exclude='vendor/velxio/docs/' \
  --exclude='vendor/velxio/test/' \
  --exclude='vendor/velxio/vscode-extension/' \
  --exclude='*.log' \
  --exclude='.env.hidden' \
  ./ "$TARGET:$APP_DIR/"

echo "==> installing/building on the VM (this can take a while the first time)"
remote_env="WIREUP_DOMAIN='$DOMAIN' WIREUP_APP_DIR='$APP_DIR'"
for name in ARDUINO_CLI_VERSION ARDUINO_AVR_CORE_VERSION RP2040_CORE_VERSION ESP32_CORE_VERSION WIREUP_SKIP_CORES; do
  value=${!name:-}
  [ -n "$value" ] && remote_env="$remote_env $name='$value'"
done
ssh -t "$TARGET" "cd '$APP_DIR' && sudo $remote_env bash '$APP_DIR/deployment/vm/bootstrap.sh'"

echo
echo "Done. https://$DOMAIN"
