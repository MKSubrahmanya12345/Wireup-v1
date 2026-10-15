# Vercel services deployment

The Vercel project contains two public-facing services:

- `app`: the Vite frontend, routed at `/(.*)` after more specific routes.
- `backend`: the FastAPI API, routed at `/api/(.*)`.

The browser already uses same-origin `/api/...` HTTP URLs and derives its hardware runtime WebSocket from the same origin, so no build-time frontend service URL is required.

## External remote simulation

`remote-simulation` remains an external, private service deployed with `deployment/remote-simulation/Dockerfile`. It is not a Vercel service because it requires native QEMU binaries, persistent subprocesses, in-memory sessions, and one long-lived worker.

Configure these variables in the Vercel backend runtime:

- `REMOTE_SIMULATION_URL`: the external gateway's HTTPS base URL.
- `REMOTE_SIMULATION_TOKEN`: the same secret of at least 32 printable non-whitespace ASCII characters configured on the gateway.

`REMOTE_SIMULATION_URL` is not a Vercel service binding in this topology because service bindings can only target another service in the same Vercel project. Never expose either variable through a `VITE_` variable or send the gateway token to the browser.

## Runtime limitations

The existing backend was designed for a persistent local/container host. A Vercel deployment still needs platform-compatible replacements for these features before they can be considered production-ready:

- project and artifact persistence currently uses local files;
- browser simulation coordination uses a persistent WebSocket and process-local maps;
- firmware compilation launches Arduino CLI and requires installed board toolchains;
- classic ESP32 local simulation uses a Windows QEMU DLL and child process;
- physical board discovery and flashing require access to the user's local USB/serial devices;
- production authentication requires a browser-compatible identity and authorization flow.

The Vercel configuration establishes service builds and public routing. It does not make those stateful or native capabilities serverless-compatible.

## Local validation

Use `vercel dev` to run the configured Vercel services together. Continue using `npm run dev` for the repository's existing Vite proxy development workflow.
