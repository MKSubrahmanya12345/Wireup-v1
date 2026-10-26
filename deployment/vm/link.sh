#!/usr/bin/env bash
# Mint a one-time beta sign-in link on the VM, for operators without an email
# provider. Run on the server:
#
#   sudo /opt/wireup/app/deployment/vm/link.sh tester@example.com
#
# The link is valid for 7 days and can only be redeemed once.
set -euo pipefail

APP_DIR=${WIREUP_APP_DIR:-/opt/wireup/app}
APP_USER=${WIREUP_APP_USER:-wireup}
APP_HOME=${WIREUP_APP_HOME:-/var/lib/wireup}
email=${1:-}
[ -n "$email" ] || { echo "usage: $0 <email>" >&2; exit 2; }
[ -x "$APP_DIR/.venv/bin/python" ] || { echo "missing venv at $APP_DIR/.venv (run bootstrap.sh)" >&2; exit 1; }

cd "$APP_DIR"
exec sudo -u "$APP_USER" -H env HOME="$APP_HOME" \
  "$APP_DIR/.venv/bin/python" -m backend.beta link "$email"
