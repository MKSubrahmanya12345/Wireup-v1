# Vercel frontend with laptop backend

The WireUp backend is stateful: it stores projects locally, launches Arduino
compilers, and owns long-lived runtime WebSockets. Run it on the laptop or a
stable host, and deploy only the Vite frontend to Vercel.

## 1. Create an HTTPS tunnel

Install Cloudflare Tunnel (`cloudflared`) on the laptop, then run:

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

Use a named tunnel and your own hostname before inviting external testers. Do
not port-forward `8000`, use an HTTP tunnel, or expose the backend without
authentication. The tunnel URL must be HTTPS because secure cross-site cookies
and browser WebSockets require it.

## 2. Configure the laptop backend

In the laptop `.env`:

```dotenv
WIREUP_BETA_AUTH_ENABLED=true
WIREUP_BETA_DEV_LOGIN=false
WIREUP_ADMIN_EMAIL=you@example.com
WIREUP_COOKIE_SECURE=true
WIREUP_COOKIE_SAMESITE=none
WIREUP_PUBLIC_APP_URL=https://your-project.vercel.app
WIREUP_ALLOWED_ORIGINS=https://your-project.vercel.app
BACKEND_PORT=8000
```

Use your actual Vercel production domain and add the preview domain only if
you intentionally want preview deployments to access the beta. Configure
`RESEND_API_KEY` and `RESEND_FROM_EMAIL`; development login must stay disabled
for external testers.

Start only the backend on the laptop:

```bash
source .venv/bin/activate
python -m backend.run
```

Keep the terminal, laptop, and tunnel process running. A tunnel URL from
`cloudflared tunnel --url` can change after restart; a named tunnel avoids
that.

## 3. Configure Vercel

The repository now includes a single launcher:

```bash
npm run subbu
```

That starts the laptop backend and local frontend together. For a public
backend tunnel, use:

```bash
npm run subbu -- --tunnel
```

After the tunnel is running, deploy the Vercel frontend with:

```bash
SUBBU_BACKEND_URL=https://your-backend-tunnel.example.com \
SUBBU_VERCEL_ORIGIN=https://your-project.vercel.app \
npm run subbu -- --deploy
```

The deploy mode builds with `VITE_API_BASE_URL`, checks the HTTPS URLs, and
then invokes the Vercel CLI. If the CLI is not installed, it uses `npx`.

In Vercel project settings, add the production environment variable:

```text
VITE_API_BASE_URL=https://your-backend-tunnel.example.com
```

Do not prefix provider keys, Resend keys, beta database paths, or monitoring
secrets with `VITE_`; Vite exposes `VITE_` values to every browser.

Deploy:

```bash
export PATH="$HOME/.local/nodejs/bin:$PATH"
npm run build
npx vercel --prod
```

The Vercel frontend uses the backend URL for HTTP requests and `wss://` for
the hardware runtime WebSocket. The existing `vercel.json` backend service is
not used by this topology when `VITE_API_BASE_URL` is set.

## 4. Verify before inviting testers

```bash
curl -i https://your-backend-tunnel.example.com/api/health
```

Then open the Vercel URL and verify:

1. The beta login page appears.
2. A magic-link email arrives.
3. Login returns to the Vercel site.
4. `/admin` loads for the admin email.
5. A project can be created.
6. The browser Network tab shows requests to the tunnel URL.
7. The runtime WebSocket is `wss://...`, not `ws://...`.
8. A compile/simulation action is attempted only while the laptop backend is running.

## Operational reality

The laptop is the production server in this setup. Sleep, shutdown, IP
changes, tunnel failure, compiler availability, and local disk loss affect
all testers. Back up `backend/data/hardware` and `backend/data/beta.sqlite3`.
For a real public beta, move the backend and persistent data to a stable
server before adding many testers.
