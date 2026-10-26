# Hosting WireUp on a cloud VM at wireup.space

This is the always-on, laptop-free deployment. One small Linux VM runs two
processes and owns the whole origin:

```
browser ──https/wss──▶ Caddy (443, Let's Encrypt)
                            │ reverse_proxy 127.0.0.1:8000
                            ▼
                     uvicorn (FastAPI)
                       ├── /api/*            JSON + NDJSON streaming
                       ├── /ws/.../runtime   hardware WebSocket bridge
                       └── /                 built SPA from dist/
                            │
                     backend/data/            SQLite + project JSON
```

No Vercel, no Docker, no Postgres, no Redis, no laptop in the request path.
Caddy terminates TLS and forwards WebSockets; uvicorn serves the SPA and API
from the same origin, so `VITE_API_BASE_URL` stays unset and there is no CORS
or mixed-content problem. Updating is one command from your laptop; the domain
never changes.

## Why a VM and not a PaaS

| option | verdict |
|---|---|
| **Oracle Cloud Always Free (Ampere A1)** | **Recommended.** 2 ARM OCPUs / 12 GB RAM / up to 200 GB block storage / 10 TB egress per month, permanently free. Oracle halved the A1 allowance from 4/24 in June 2026, so use **2 OCPU / 12 GB**. Compiles ESP32 comfortably. |
| GCP free e2-micro | Free but 1 GB RAM and ~1 GB/month egress. The SPA is ~100 MB, so egress runs out fast and ESP32 compiles may OOM. Needs swap. |
| Hetzner CX22 / Fly.io | Reliable and cheap (~€4 / ~$5 per month), not free. Good fallback if Oracle capacity is unavailable. |
| Render / Koyeb free | Sleep after idle, ephemeral disk, no persistent volumes. Breaks long compiles and loses tester data. |
| Cloudflare Workers/Pages | Cannot run Python, `arduino-cli`, or a long-lived WebSocket bridge. |

The Oracle signup needs a card for identity verification (Always Free resources
are not charged), and A1 capacity is sometimes unavailable in a region. If
creation fails with "out of capacity", try another availability domain/region
or retry later. Keep the instance in your **home region** — Always Free
resources outside it are billed.

Two Oracle caveats worth knowing before committing:

- **Idle reclamation.** Oracle may reclaim Always Free compute that sits below
  roughly 20% CPU for seven days. A beta that is mostly idle can qualify.
  Upgrading the account to Pay-As-You-Go removes that behavior while the usage
  stays inside Always Free limits (verify current terms in the console), or you
  accept the risk and keep off-VM backups.
- **50 Mbps internet ceiling** on Always Free networking. Fine for a handful of
  testers; the 100 MB SPA takes a few seconds on a cold cache.

## 1. Create the VM (Oracle Cloud)

1. Create an account at <https://cloud.oracle.com> and pick the region closest
   to your testers (this becomes the home region).
2. **Compute → Instances → Create instance**
   - Image: **Ubuntu 24.04**
   - Shape: **Ampere → VM.Standard.A1.Flex**, **2 OCPUs / 12 GB**
     (this is the current Always Free allowance)
   - Boot volume: **100 GB** (ESP32 core + node_modules + dist need ~7 GB;
     100 GB leaves room for project data and backups)
   - Networking: create/choose a VCN with a **public subnet**, and enable
     **Assign a public IPv4 address**
   - SSH keys: upload your public key (`~/.ssh/id_ed25519.pub`)
3. After it boots, reserve the IP: **Networking → IP management → Reserved
   public IPs**, or on the instance's VNIC turn the public IP into a reserved
   one. An ephemeral IP changes if the instance is stopped/started; a reserved
   IP keeps DNS pointing at it.
4. Open the ports:
   - **VCN → Security Lists → Default Security List → Add ingress rules**
   - Source `0.0.0.0/0`, TCP **80** and **443** (keep 22 for SSH)
   - Do **not** expose 8000; uvicorn only listens on loopback.
5. Connect once:

   ```bash
   ssh -i ~/.ssh/id_ed25519 ubuntu@<public-ip>
   ```

`deployment/vm/bootstrap.sh` also inserts the iptables ACCEPT rules that Oracle's
Ubuntu images need; the security-list rules above are still required.

## 2. Point the domain at it (Namecheap, no nameserver change)

`wireup.space` stays on Namecheap BasicDNS. You do **not** have to move the zone
to Cloudflare.

**Namecheap → Domain List → wireup.space → Manage → Advanced DNS**

| type | host | value | ttl |
|---|---|---|---|
| A Record | `@` | `<public-ip>` | Automatic |
| A Record | `www` | `<public-ip>` | Automatic |

Delete any conflicting `A`, `AAAA`, `CNAME`, or URL-redirect records for `@` and
`www`. Caddy obtains and renews a Let's Encrypt certificate automatically once
port 80/443 reach the VM (HTTP-01). Propagation is usually a few minutes.

Optional later: put Cloudflare in front for caching/DDoS protection by changing
the Namecheap nameservers to Cloudflare's. Not required for this setup.

## 3. Deploy from your laptop

The scripts sync the working tree over SSH, so you do not need to commit or
push first. `.env` goes with it; `backend/data/` never does.

```bash
chmod +x deployment/vm/*.sh
deployment/vm/deploy.sh ubuntu@<public-ip>
```

What happens on the first run (10–25 minutes, mostly ESP32 core download):

- apt packages, Node.js 22, Caddy
- `arduino-cli` 1.5.1 matched to x86_64 **or** aarch64
- cores `arduino:avr@1.8.6`, `rp2040:rp2040@4.5.2`, `esp32:esp32@3.2.0`
  (override with `ARDUINO_AVR_CORE_VERSION`, `RP2040_CORE_VERSION`,
  `ESP32_CORE_VERSION`, or `ARDUINO_CLI_VERSION`; `WIREUP_SKIP_CORES=true`
  skips the long first-run download)
- `npm ci && npm run build` → `dist/`
- Python venv + `backend/requirements.txt`
- `wireup.service` (uvicorn, `WIREUP_RELOAD=0`, one worker) and Caddy
- nightly backup timer

Later deploys only upload changed files, reinstall if `package-lock.json` /
requirements changed, rebuild, and restart:

```bash
deployment/vm/deploy.sh ubuntu@<public-ip>     # ~30–60 s
```

Check it:

```bash
curl -i https://wireup.space/api/health
ssh ubuntu@<public-ip> 'systemctl status wireup caddy'
ssh ubuntu@<public-ip> 'journalctl -u wireup -f'
```

If DNS has not propagated yet, `bootstrap.sh` still finishes and prints a
warning; Caddy will keep retrying the certificate until the name resolves.

## 4. Create the admin sign-in link

There is no email provider configured, which is deliberate for a zero-budget
beta. Mint a one-time link on the server and open it in your browser:

```bash
ssh ubuntu@<public-ip> 'sudo /opt/wireup/app/deployment/vm/link.sh mksubbu007@gmail.com'
```

The email in `WIREUP_ADMIN_EMAIL` in `.env` becomes the `admin` account; every
other email becomes an isolated `tester` workspace. Links are valid for 7 days
and can be redeemed once.

## 5. Invite testers

Sign in as admin, open **`/admin`**, enter a tester's email, press **Invite**,
then **Copy link** and send it to them yourself (WhatsApp, email, anything).
`GET /api/beta/invite` is admin-only and returns the link even when no email
provider is configured.

Each tester gets their own `workspace_id`. Projects are filtered per workspace
and cross-workspace reads return `404`; verified with two live sessions.

If you later add a free Resend account (`RESEND_API_KEY` +
`RESEND_FROM_EMAIL`), the same invite is emailed automatically and the admin
still gets the copyable link as a fallback.

## 6. Backups and data

Live data lives only on the VM:

```
/opt/wireup/app/backend/data/beta.sqlite3       users, sessions, invites, events
/opt/wireup/app/backend/data/hardware/*.json    projects
```

`wireup-backup.timer` snapshots both nightly at 04:30 into
`/var/backups/wireup/` (14-day retention) using SQLite's online backup API.
Manual run and offsite copy:

```bash
ssh ubuntu@<public-ip> 'sudo /usr/local/bin/wireup-backup'
scp ubuntu@<public-ip>:/var/backups/wireup/wireup-*.tar.gz ~/wireup-backups/
```

A deploy never uploads or deletes `backend/data/`.

## 7. Day-2 operations

| task | command |
|---|---|
| update code | `deployment/vm/deploy.sh ubuntu@<ip>` |
| backend logs | `ssh ubuntu@<ip> 'journalctl -u wireup -f'` |
| ingress logs | `ssh ubuntu@<ip> 'journalctl -u caddy -f'` |
| restart app | `ssh ubuntu@<ip> 'sudo systemctl restart wireup'` |
| shell/config | `ssh ubuntu@<ip> 'sudo -u wireup -H env HOME=/var/lib/wireup bash'` |
| disk usage | `ssh ubuntu@<ip> 'df -h /; du -sh /opt/wireup/app/backend/data'` |

`.env` changes require `sudo systemctl restart wireup` (the app loads `.env`
with `override=True`; the reloader does not re-read it).

## 8. Limits worth knowing

- **One uvicorn worker.** Runtime WebSocket sessions and the compile semaphore
  are process-local. Do not add `--workers`.
- **Compiles are serialized** (global `asyncio.Semaphore(1)`). Fine for a
  handful of testers; the second compile waits. 2 ARM cores still beat most
  laptops for this workload.
- **No native QEMU on the VM.** Classic ESP32 boards (`esp32-devkit-*`) use
  the `qemu-native` runtime, which depends on a Windows-only QEMU DLL and the
  vendored 2.0.17 profile; compilation for those boards fails with a clear
  `503` just as it does on the current laptop. ESP32-S3/C3 and the AVR/RP2040
  boards use the in-browser emulators and work normally.
- **Compilation is not sandboxed.** `arduino-cli` runs as the `wireup` user.
  Workspace isolation is enforced by the API, not by the OS: that user can also
  read `/opt/wireup/app/.env` (provider keys) and every tester's project JSON.
  Only invite people you trust with code execution equivalent to that user. For
  untrusted testers, move compilation to a separate non-privileged worker.
- **`WIREUP_BETA_DEV_LOGIN` must stay `false`.** Invite links and the admin CLI
  are the only ways in.
- **Oracle Always Free is generous but not a contract.** Keep the nightly
  backups and copy them off the VM. If capacity or terms become a problem, the
  same scripts work unchanged on a $4 Hetzner/Fly VM.
- **Egress:** the SPA is ~100 MB per cold load (Monaco, WASM, firmware
  binaries). Oracle's 10 TB/month is plenty. On GCP's free 1 GB, put Cloudflare
  in front or use a paid host.

## 9. Alternatives in one line each

- **GCP e2-micro** (free, `us-west1`/`us-central1`/`us-east1`): create Ubuntu
  24.04, add a 2 GB swap file, run `deployment/vm/bootstrap.sh`; expect slow
  ESP32 compiles and watch the egress cap.
- **Hetzner CX22** (~€4/month): same Ubuntu 24.04 image, same script, no
  capacity lottery.
- **Fly.io** (~$5/month): requires the Docker path in
  `deployment/local-production/` rather than this script.

## 10. Troubleshooting

| symptom | check |
|---|---|
| `https://wireup.space` times out | DNS A record, Oracle security list 80/443, `sudo iptables -S INPUT` |
| certificate never issues | `journalctl -u caddy -n 80`; port 80 must be reachable from the internet |
| `502 Bad Gateway` | `systemctl status wireup`; `journalctl -u wireup -n 80` |
| WebSocket disconnects | Caddy handles upgrades automatically; confirm the page is loaded over `https://`, not a stale `http://` origin |
| compile says Arduino CLI unavailable | `sudo -u wireup -H env HOME=/var/lib/wireup arduino-cli version`; check `ARDUINO_CLI_PATH` in the unit |
| `npm ci` fails for lack of memory | use a 2 GB+ VM or add swap (`fallocate -l 2G /swapfile && mkswap /swapfile && swapon /swapfile`) |
| forgot admin link | re-run `link.sh`; old links stay valid until used/expired |
