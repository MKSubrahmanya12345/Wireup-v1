#!/usr/bin/env bash
# WireUp production host setup for Ubuntu 24.04 (x86_64 or aarch64).
#
# Run this on the VM (it is idempotent, so re-running it deploys an update):
#
#   sudo WIREUP_DOMAIN=wireup.space bash /opt/wireup/app/deployment/vm/bootstrap.sh
#
# Normal use is from your laptop instead:
#
#   deployment/vm/deploy.sh ubuntu@<vm-ip>
#
# It installs OS packages, Arduino CLI and the AVR/Pico/ESP32 cores, the Python
# venv, the SPA build, and the systemd + Caddy services. Application data in
# backend/data/ is never touched by this script.
set -euo pipefail

APP_DIR=${WIREUP_APP_DIR:-/opt/wireup/app}
APP_USER=${WIREUP_APP_USER:-wireup}
APP_HOME=${WIREUP_APP_HOME:-/var/lib/wireup}
DOMAIN=${WIREUP_DOMAIN:-wireup.space}
ARDUINO_CLI_VERSION=${ARDUINO_CLI_VERSION:-1.5.1}
AVR_CORE=${ARDUINO_AVR_CORE_VERSION:-1.8.6}
RP2040_CORE=${RP2040_CORE_VERSION:-4.5.2}
ESP32_CORE=${ESP32_CORE_VERSION:-3.2.0}
SKIP_CORES=${WIREUP_SKIP_CORES:-false}
NODE_MAJOR=${WIREUP_NODE_MAJOR:-22}

step() { printf '\n=== %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo"
[ -d "$APP_DIR/backend" ] || die "repo not found at $APP_DIR (run deployment/vm/deploy.sh from your laptop first)"
[ -f "$APP_DIR/.env" ] || die "missing $APP_DIR/.env; deploy with deployment/vm/deploy.sh so the server-only settings come along"
if grep -qE '^WIREUP_BETA_DEV_LOGIN=true' "$APP_DIR/.env"; then
  printf 'warning: WIREUP_BETA_DEV_LOGIN=true lets anyone sign in as an invited email.\n'
fi
if ! grep -qE '^WIREUP_BETA_AUTH_ENABLED=true' "$APP_DIR/.env"; then
  printf 'warning: WIREUP_BETA_AUTH_ENABLED is not true; API routes would be public.\n'
fi

if [ -r /etc/os-release ]; then
  . /etc/os-release
  [ "${ID:-}" = ubuntu ] || printf 'warning: tested on Ubuntu 24.04, found %s %s\n' "${ID:-?}" "${VERSION_ID:-?}"
fi

step "System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip \
  curl ca-certificates gnupg git rsync \
  build-essential unzip xz-utils jq

install_caddy() {
  if apt-get install -y --no-install-recommends caddy; then
    return 0
  fi
  printf 'caddy was not in the distro archive; adding the official repository\n'
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y --no-install-recommends caddy
}
install_caddy

node_major=$(node -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)
if [ "${node_major:-0}" -lt "$NODE_MAJOR" ]; then
  step "Node.js ${NODE_MAJOR}"
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
  apt-get install -y --no-install-recommends nodejs
fi
printf 'node %s / npm %s\n' "$(node -v)" "$(npm -v)"

step "Application user and directories"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --create-home --home-dir "$APP_HOME" --user-group --shell /usr/sbin/nologin "$APP_USER"
fi
install -d -m 755 -o "$APP_USER" -g "$APP_USER" \
  "$APP_HOME" "$APP_DIR/backend/data" "$APP_DIR/backend/builds"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
if [ -f "$APP_DIR/.env" ]; then
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
fi

run_as_app() {
  sudo -u "$APP_USER" -H env HOME="$APP_HOME" "$@"
}

if ! command -v arduino-cli >/dev/null 2>&1; then
  step "Arduino CLI ${ARDUINO_CLI_VERSION}"
  case "$(uname -m)" in
    x86_64|amd64) cli_arch=Linux_64bit ;;
    aarch64|arm64) cli_arch=Linux_ARM64 ;;
    *) die "unsupported architecture for arduino-cli: $(uname -m)" ;;
  esac
  asset="arduino-cli_${ARDUINO_CLI_VERSION}_${cli_arch}.tar.gz"
  archive=$(mktemp)
  trap 'rm -f "$archive"' EXIT
  curl -fsSL "https://github.com/arduino/arduino-cli/releases/download/v${ARDUINO_CLI_VERSION}/${asset}" -o "$archive"
  # GitHub publishes per-asset digests on the release API; verify when available.
  expected=$(curl -fsSL "https://api.github.com/repos/arduino/arduino-cli/releases/tags/v${ARDUINO_CLI_VERSION}" 2>/dev/null \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(next((a.get("digest","") for a in d.get("assets",[]) if a.get("name")==sys.argv[1]),""))' "$asset" 2>/dev/null || true)
  if [ -n "$expected" ]; then
    actual="sha256:$(sha256sum "$archive" | cut -d' ' -f1)"
    [ "$actual" = "$expected" ] || die "arduino-cli digest mismatch: expected $expected, got $actual"
  else
    printf 'warning: could not fetch a checksum for %s; continuing\n' "$asset"
  fi
  tar -xzf "$archive" -C /usr/local/bin arduino-cli
  chmod 755 /usr/local/bin/arduino-cli
  rm -f "$archive"
  trap - EXIT
fi
arduino-cli version

if [ "$SKIP_CORES" != "true" ]; then
  step "Arduino cores (${AVR_CORE} / rp2040 ${RP2040_CORE} / esp32 ${ESP32_CORE})"
  if [ ! -f "$APP_HOME/.arduino15/arduino-cli.yaml" ]; then
    run_as_app arduino-cli config init
  fi
  add_board_url() {
    local url=$1
    if ! run_as_app arduino-cli config get board_manager.additional_urls 2>/dev/null | grep -qF "$url"; then
      run_as_app arduino-cli config add board_manager.additional_urls "$url"
    fi
  }
  add_board_url https://github.com/earlephilhower/arduino-pico/releases/download/global/package_rp2040_index.json
  add_board_url https://espressif.github.io/arduino-esp32/package_esp32_index.json
  run_as_app arduino-cli core update-index
  run_as_app arduino-cli core install "arduino:avr@${AVR_CORE}"
  run_as_app arduino-cli core install "rp2040:rp2040@${RP2040_CORE}"
  run_as_app arduino-cli core install "esp32:esp32@${ESP32_CORE}"
  run_as_app arduino-cli core list
fi

step "Frontend build"
run_as_app bash -c "cd '$APP_DIR' && npm ci --no-audit --no-fund && npm run build"

step "Python environment"
run_as_app bash -c "cd '$APP_DIR' && python3 -m venv .venv \
  && .venv/bin/python -m pip install --quiet --upgrade pip \
  && .venv/bin/python -m pip install --quiet -r backend/requirements.txt"

step "systemd service"
cat > /etc/systemd/system/wireup.service <<EOF
[Unit]
Description=WireUp backend (FastAPI + SPA + hardware WebSocket)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment=HOME=$APP_HOME
Environment=PYTHONUNBUFFERED=1
Environment=WIREUP_RELOAD=0
Environment=ARDUINO_CLI_PATH=/usr/local/bin/arduino-cli
ExecStart=$APP_DIR/.venv/bin/python -m backend.run
Restart=always
RestartSec=3
TimeoutStopSec=30
KillSignal=SIGINT
NoNewPrivileges=true
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable wireup.service >/dev/null

step "Caddy (automatic HTTPS for $DOMAIN)"
install -d -m 755 /etc/wireup
cat > /etc/wireup/caddy.env <<EOF
WIREUP_DOMAIN=$DOMAIN
EOF
install -d -m 755 /etc/systemd/system/caddy.service.d
cat > /etc/systemd/system/caddy.service.d/wireup.conf <<'EOF'
[Service]
EnvironmentFile=/etc/wireup/caddy.env
EOF
install -m 644 "$APP_DIR/deployment/vm/Caddyfile" /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl daemon-reload
systemctl enable caddy.service >/dev/null 2>&1 || true

step "Backups"
install -m 755 "$APP_DIR/deployment/vm/backup.sh" /usr/local/bin/wireup-backup
cat > /etc/systemd/system/wireup-backup.service <<'EOF'
[Unit]
Description=WireUp data backup

[Service]
Type=oneshot
ExecStart=/usr/local/bin/wireup-backup
EOF
cat > /etc/systemd/system/wireup-backup.timer <<'EOF'
[Unit]
Description=Nightly WireUp data backup

[Timer]
OnCalendar=*-*-* 04:30:00
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now wireup-backup.timer >/dev/null

step "Firewall"
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 22/tcp >/dev/null 2>&1 || true
  ufw allow 80/tcp >/dev/null 2>&1 || true
  ufw allow 443/tcp >/dev/null 2>&1 || true
fi
# Oracle Cloud Ubuntu images ship a persistent iptables ruleset that drops 80/443.
if command -v iptables >/dev/null 2>&1; then
  for port in 80 443; do
    if ! iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
      iptables -I INPUT 1 -p tcp --dport "$port" -j ACCEPT \
        || printf 'warning: could not add an iptables rule for port %s\n' "$port"
    fi
  done
  command -v netfilter-persistent >/dev/null 2>&1 && netfilter-persistent save >/dev/null 2>&1 || true
  if [ -d /etc/iptables ] && command -v iptables-save >/dev/null 2>&1; then
    iptables-save > /etc/iptables/rules.v4 || true
  fi
fi

step "Starting services"
systemctl restart wireup.service
systemctl restart caddy.service
for _ in $(seq 1 30); do
  curl -fsS --max-time 2 http://127.0.0.1:8000/api/health >/dev/null 2>&1 && break
  sleep 2
done
if ! curl -fsS --max-time 4 http://127.0.0.1:8000/api/health; then
  journalctl -u wireup.service -n 50 --no-pager || true
  die "backend did not become healthy"
fi
printf '\n'

step "Public check"
if getent ahostsv4 "$DOMAIN" >/dev/null 2>&1; then
  printf 'waiting for the certificate for %s (up to 60s)...\n' "$DOMAIN"
  for _ in $(seq 1 20); do
    curl -fsS --max-time 4 "https://$DOMAIN/api/health" >/dev/null 2>&1 && break
    sleep 3
  done
  curl -fsS --max-time 8 "https://$DOMAIN/api/health" \
    && printf '\nhttps://%s is live\n' "$DOMAIN" \
    || printf 'warning: https://%s is not answering yet. Check DNS (A record) and cloud firewall ports 80/443, then: journalctl -u caddy -n 50\n' "$DOMAIN"
else
  printf 'warning: %s has no A/AAAA record from this host yet; skipping the public check.\n' "$DOMAIN"
fi

cat <<EOF

Host setup complete.
  app:      $APP_DIR
  data:     $APP_DIR/backend/data (gitignored, never overwritten by deploys)
  service:  systemctl status wireup
  logs:     journalctl -u wireup -f
  ingress:  systemctl status caddy
  backups:  systemctl list-timers wireup-backup.timer

Create a sign-in link for the beta admin (WIREUP_ADMIN_EMAIL in .env):
  sudo $APP_DIR/deployment/vm/link.sh <admin-email>
EOF
