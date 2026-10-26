#!/usr/bin/env bash
# Install the laptop host as a user-level systemd service (no root required).
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
python="$repo/.venv/bin/python"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
cloudflared=$(command -v cloudflared || true)

[ -x "$python" ] || { echo "Missing $python. Create the venv first: python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt"; exit 1; }
if [ ! -f "$repo/dist/index.html" ]; then
  echo "Missing dist/index.html. Build the frontend first: npm run build"
  exit 1
fi
mkdir -p "$unit_dir"

cat > "$unit_dir/wireup-backend.service" <<EOF
[Unit]
Description=WireUp backend and SPA (laptop host)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$repo
Environment=WIREUP_RELOAD=0
Environment=PYTHONUNBUFFERED=1
ExecStart=$python -m backend.run
Restart=always
RestartSec=3
TimeoutStopSec=30

[Install]
WantedBy=default.target
EOF

if [ -n "$cloudflared" ]; then
  cat > "$unit_dir/wireup-tunnel.service" <<EOF
[Unit]
Description=Cloudflare tunnel for wireup.space
After=network-online.target wireup-backend.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=$cloudflared --no-autoupdate --config %h/.cloudflared/wireup.yml tunnel run
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
else
  echo "cloudflared not found in PATH; skipping tunnel unit."
fi

systemctl --user daemon-reload
systemctl --user enable --now wireup-backend.service

cat > "$unit_dir/wireup-backup.service" <<EOF
[Unit]
Description=WireUp data backup

[Service]
Type=oneshot
ExecStart=$repo/deployment/laptop/backup.sh
EOF
cp "$repo/deployment/laptop/wireup-backup.timer" "$unit_dir/wireup-backup.timer"
systemctl --user daemon-reload
systemctl --user enable --now wireup-backup.timer

if [ -f "$HOME/.cloudflared/wireup.yml" ]; then
  systemctl --user enable --now wireup-tunnel.service
else
  echo "No ~/.cloudflared/wireup.yml yet; tunnel unit is installed but not started."
fi
loginctl enable-linger "$USER" >/dev/null 2>&1 || echo "Run 'loginctl enable-linger $USER' once so services start before login."

echo
echo "Installed. Useful commands:"
echo "  systemctl --user status wireup-backend wireup-tunnel"
echo "  journalctl --user -u wireup-backend -f"
echo "  systemctl --user restart wireup-backend   # after npm run build"
