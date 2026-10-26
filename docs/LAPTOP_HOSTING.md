> **Superseded.** The laptop is no longer the host. Use
> [`CLOUD_VM_HOSTING.md`](CLOUD_VM_HOSTING.md) for the always-on cloud VM
> deployment. This page is kept only as a reference for the tunnel/DNS details.

# Hosting WireUp on your laptop at wireup.space

One origin, one process, zero hosting cost:

```
browser ──https──▶ Cloudflare edge (free TLS, wireup.space)
                         │ named Cloudflare Tunnel (WebSockets supported)
                         ▼
              cloudflared on the laptop ──▶ 127.0.0.1:8000
                                              │
                            uvicorn: /api/*  +  dist/ SPA
```

Why this and not a quick tunnel: quick tunnels (`*.trycloudflare.com`) answer
every WebSocket upgrade with **403**, so the browser hardware runtime can never
connect, and their hostname changes on every restart. A named tunnel on your own
domain fixes both.

## 1. Move wireup.space to Cloudflare (once)

`wireup.space` currently uses Namecheap's `dns1/dns2.registrar-servers.com`.
A named tunnel needs the zone in Cloudflare.

1. Cloudflare dashboard → **Add a site** → `wireup.space` → Free plan.
2. Copy the two assigned nameservers (`xxx.ns.cloudflare.com`).
3. Namecheap → Domain List → `wireup.space` → **Nameservers** → Custom DNS →
   paste both → save.
4. Wait for Cloudflare to report the zone **Active** (usually minutes).

## 2. Create the tunnel (once)

```bash
cloudflared tunnel login                 # opens a browser, pick wireup.space
cloudflared tunnel create wireup         # prints the tunnel UUID
cloudflared tunnel route dns wireup wireup.space
cloudflared tunnel route dns wireup www.wireup.space
```

Create `~/.cloudflared/wireup.yml` from
`deployment/laptop/cloudflared.yml.example` and replace `<TUNNEL-UUID>`.

Test it before installing the service:

```bash
cloudflared --no-autoupdate --config ~/.cloudflared/wireup.yml tunnel run
```

## 3. Backend configuration

`.env` (already set in this repo):

```dotenv
WIREUP_BETA_AUTH_ENABLED=true
WIREUP_BETA_DEV_LOGIN=false          # only the admin CLI / invite links issue sessions
WIREUP_ADMIN_EMAIL=you@example.com
WIREUP_COOKIE_SECURE=true
WIREUP_COOKIE_SAMESITE=none
WIREUP_PUBLIC_APP_URL=https://wireup.space
WIREUP_ALLOWED_ORIGINS=https://wireup.space
BACKEND_PORT=8000
```

`VITE_API_BASE_URL` must be **unset** for this topology: the SPA is served by the
backend, so `apiUrl()` resolves to same-origin `/api/...` and `wss://wireup.space`
for the runtime.

Build and start:

```bash
npm run build
deployment/laptop/install.sh          # user units, no sudo
```

## 4. Keep the laptop serving

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
loginctl enable-linger "$USER"
```

Check:

```bash
systemctl --user status wireup-backend wireup-tunnel
journalctl --user -u wireup-backend -f
curl -i https://wireup.space/api/health
```

## 5. Inviting testers (no email provider needed)

Every tester gets their own workspace; projects are filtered by `workspace_id`
and cross-workspace reads return 404.

- **Admin UI**: sign in as the admin, open `/admin`, enter the tester's email,
  press **Invite**, then **Copy link** and send it over WhatsApp/email yourself.
  Links live 7 days and are single-use.
- **Terminal**: `.venv/bin/python -m backend.beta link tester@example.com`

If you later add `RESEND_API_KEY` and `RESEND_FROM_EMAIL` (free tier), the same
invite is emailed automatically and the link is still returned to the admin.

Sign in as admin without any email provider:

```bash
.venv/bin/python -m backend.beta link mksubbu007@gmail.com
```

The admin email in `WIREUP_ADMIN_EMAIL` is the only account created with the
`admin` role.

## 6. Updating the running app

```bash
git pull
npm run build
systemctl --user restart wireup-backend
```

Backend-only changes need only the restart. The tunnel process does not need to
restart, so the public URL never changes.

## 7. Backups

`deployment/laptop/install.sh` enables a nightly timer. Manual run:

```bash
deployment/laptop/backup.sh            # ~/wireup-backups/wireup-<stamp>.tar.gz
```

It snapshots `backend/data/beta.sqlite3` through SQLite's backup API (safe while
running) plus `backend/data/hardware/`, and keeps 14 days.

## 8. What the laptop has to carry

Verified on this machine: 12 threads, 15 GB RAM, Arduino CLI 1.5.1 with
`arduino:avr`, `rp2040:rp2040`, `esp32:esp32` installed.

- Compilation is serialized by a global semaphore (one compiler at a time).
  That is fine for a handful of testers; a compile queue is the first thing that
  will feel slow with a crowd.
- Browser simulations run in each tester's own browser; the backend only bridges
  commands over the runtime WebSocket.
- Native ESP32 QEMU (`qemu-native`) is unavailable here (no `qemu-system-xtensa`),
  so ESP32 uses the in-browser emulator.
- The backend is one process with process-local runtime sessions: do not run
  multiple uvicorn workers. `WIREUP_RELOAD=0` is set by the unit so the process
  never restarts mid-compile.

## 9. Security notes for an invite-only beta

- API routes require a beta session (`401` otherwise); the SPA shell is public.
- SQLite sessions are HttpOnly cookies, 14-day TTL.
- Compilation is **not** sandboxed: it runs as your user. Only invite people you
  trust with code execution, or move compilation to a disposable worker later.
- `WIREUP_BETA_DEV_LOGIN` must stay `false` once testers are external: it lets
  anyone who knows an invited address sign in as that person.
- Keep `backend/data/` out of git (it is already ignored) and out of backups you
  share.
