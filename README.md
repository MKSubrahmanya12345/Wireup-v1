# WireUp hardware prototyping platform

WireUp combines a dark Vite/React/TypeScript interface, circuit and schematic workspaces, real Arduino compilation, Velxio CPU emulation, ngspice analog analysis, and a Python LangGraph/Groq hardware agent.

## Start

```powershell
npm install
uv venv .venv
uv pip install --python .venv/Scripts/python.exe -r backend/requirements.txt
npm run dev
```

Existing dependencies are installed here. Open http://127.0.0.1:5173. The root prompt creates a prototype and opens its project-scoped agent and canvas. Existing prototypes appear on the home page; `/projects` redirects there. `/assistant` and `/chat/:id` preserve earlier chat access; `/orb` preserves the background demo.

Without uv: `python -m venv .venv`, then `.venv\Scripts\python.exe -m pip install -r backend/requirements.txt`. The npm Python launcher supports Windows and Unix virtual environments.

Windows backend code edits require restarting `npm run dev:api`; reload is disabled there to keep the Proactor event loop required by compiler subprocesses. Frontend changes reload normally. `npm run dev:web` and `npm run dev:api` can run separately.

## Groq environment

Root `.env` contains server-only settings:

```dotenv
GROQ_API_KEY=your_key_here
GROQ_MODEL=openai/gpt-oss-120b
NVIDIA_API_KEY=your_nvidia_key_here
NVIDIA_MODEL=z-ai/glm-5.3
BACKEND_PORT=8000
```

Key/model edits apply on the next request. Port edits require restarting. Never use a `VITE_` API-key variable. `.env` is ignored; `.env.example` contains no key. Groq rate limits can block real builds even with a valid key. The agent retries the same model call at most twice with bounded waiting and never repeats already-completed tool mutations automatically. Check https://console.groq.com/settings/limits. A completed local test is not evidence that a provider quota problem is resolved.

## Sarvam AI

Sarvam uses its OpenAI-compatible v2 endpoint. Set `SARVAM_API_KEY` and
`SARVAM_MODEL` (default `sarvam-105b`) to expose it in the model selector.
`SARVAM_BASE_URL` defaults to `https://api.sarvam.ai/v2`. The backend keeps the
key server-side and sends Sarvam's `api-subscription-key` header.

## Beta monitoring

The beta event log remains in `backend/data/beta.sqlite3`. Optional hosted
observability can be enabled without changing product code:

```dotenv
SENTRY_DSN=
POSTHOG_API_KEY=
POSTHOG_HOST=https://app.posthog.com
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com
```

Sentry receives backend exceptions, PostHog receives redacted product events,
and Langfuse receives redacted model-run events. Prompts, firmware source,
runtime tokens, and API keys are excluded from forwarded monitoring metadata.

For real passwordless tester emails, configure `RESEND_API_KEY`,
`RESEND_FROM_EMAIL`, and `WIREUP_PUBLIC_APP_URL`. Without those values, local
development can use `WIREUP_BETA_DEV_LOGIN=true`; production should leave that
development shortcut disabled.

The **Chat model** selector in every chat composer switches between configured Groq and NVIDIA models and remembers the choice in this browser. NVIDIA uses the fixed OpenAI-compatible endpoint `https://integrate.api.nvidia.com/v1` through LangChain `ChatOpenAI`, retaining the same LangGraph hardware tools. The selector is disabled during generation. The configured model must support tools and be available to your NVIDIA account; a 403 response requires resolving provider/model access. Keys stay server-side and are never included in model-list responses.

## Azure

Azure is available in the chat model selector. Set these server-only values in `.env`:

```dotenv
AZURE_API_KEY=your_azure_key
AZURE_MODEL_ID=gpt-4
AZURE_BASE_URL=
```

The model value must match the deployed model name accepted by your Azure resource; change it if your deployment uses a different name. The integration uses Azure's OpenAI-compatible v1 API through LangChain `ChatOpenAI`, with streaming and the existing LangGraph tools. No API-version query is added for this v1 endpoint. Temperature is omitted for compatibility with reasoning models. A live response is unverified until a valid resource key and accessible deployment are supplied. Keys are never sent to the browser.

## Amazon Bedrock

The selector also includes Amazon Bedrock. Configure `AWS_REGION=eu-north-1` and `BEDROCK_MODEL_ID=moonshotai.kimi-k2.5`. `BEDROCK_AUTH_MODE=iam` uses `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and optional `AWS_SESSION_TOKEN`; `api_key` explicitly uses `BEDROCK_API_KEY`. Authentication modes are not silently mixed.

`BEDROCK_MAX_TOKENS` is capped by `BEDROCK_MAX_TOKENS_CEILING` (at most 160,000). The supplied 1,000,000 request therefore becomes 160,000 output tokens. This is not an input-context setting or proof of the provider's actual output allowance. Lower it to a supported value if AWS rejects inference parameters. Temperature/top-p, client timeout, and retries are read from the supplied Bedrock settings. Existing request-level limits still bound ordinary chat to 300 seconds and hardware agent runs to 300 seconds; the 1,200,000 ms client setting does not make the overall run last 20 minutes. `BEDROCK_VALIDATION_MODEL_ID` and `BEDROCK_FIXER_MODEL_ID` are saved for future separate passes; the current agent does not run additional validation/fixer models automatically.

AWS metadata confirms Kimi K2.5 exists and supports streaming in this region. Live inference tests returned invalid API-key authentication in bearer mode and `ValidationException: Operation not allowed` with IAM credentials. Resolve AWS account/model inference permissions before expecting replies. The Converse adapter maps required tool selection to AWS's `any` format. Keys are private and never returned in health/model lists.

## Velxio examples and starters

All 321 evaluated upstream examples are analyzed in `backend/templates/example-analysis.json`; each records board/component/library/language requirements and known compatibility gaps. `backend/templates/FEATURE_REQUIREMENTS.md` describes features derived from the collection and backend/runtime interfaces for the workspace UI owner. This is an analyzed source snapshot, not model training or proof all 321 projects simulate.

The home page's **View all examples** opens `/examples`, a searchable board-filtered gallery of all 321 records. Six compatible starters can be opened; the others expose dependency/capability requirements and are not advertised as runnable. Opening a starter initializes the project chat with an immediate template-derived setup guide describing the circuit, exact wires, compile/run steps, expected interaction, and physical assembly precautions. It is labelled as generated from the template rather than a live AI response, so setup help works without a model key.

Six home-page starters open independent prewired copies: Serial Hello World, Uno Button Control, Nano Button + LED, Mega Blink, Pico Blink and Pico Button + LED. They preserve upstream firmware and attribution; Pico Blink uses the onboard GPIO25 LED because its upstream external LED layout targets a non-exposed pin. Samples choose an explicit board as part of selecting a known template; new freeform projects still start empty.

`GET /api/hardware/samples` lists starters and requirements; `POST /api/hardware/samples/{id}/open` creates a revision-validated copy without an AI call. The agent can use `search_example_requirements` to consult all analyzed prototypes. Unsupported libraries, peripherals, multi-file/multi-board projects and networking are not silently imported. See the analysis for each blocker.

## Workspace

## Invite-only beta mode

For the always-on deployment, see
[Cloud VM hosting at wireup.space](./docs/CLOUD_VM_HOSTING.md). One Ubuntu VM
runs Caddy + uvicorn, serves the built SPA and `/api` from the same origin, and
needs no laptop, Docker, or external database. The older
[Vercel frontend with laptop backend](./docs/LAPTOP_BACKEND_VERCEL.md) flow is
kept as a reference only.

Set `WIREUP_BETA_AUTH_ENABLED=true` to require an invited tester session for API
access. Beta users receive an isolated workspace and projects are scoped to that
workspace. `WIREUP_BETA_DEV_LOGIN` must stay `false` for external testers; issue
one-time links with `python -m backend.beta link <email>` or from `/admin`.

The beta service stores users, invitations, sessions, and structured product
events in the SQLite file configured by `WIREUP_BETA_DB`. Set
`WIREUP_ADMIN_EMAIL` before the first development login to create the initial
admin account. Admin endpoints are available at `/api/beta/admin/summary`.

- Large circuit canvas, categorized visual catalog, right-side agent, floating fit/zoom controls, and compact collapsible console.
- Circuit/Schematic representations of the same project; no Builder/Studio switch.
- Sketch drawer for firmware editing; save/undo, real Compile, Run/Stop, serial and diagnostics.
- Canonical project data persists under ignored `backend/data/hardware/`; revision checks prevent stale edits from overwriting changes.
- Supported local MCU targets: Arduino Uno, Nano, Mega, Raspberry Pi Pico, and limited Pico W CPU/external GPIO/UART0 (no CYW43 wireless or onboard LED). Click a supported board in the catalog to replace the current MCU while preserving stable `board` ID. Remove attached wires first; replacement invalidates compilation. Multiple simultaneously emulated MCUs are not supported by this project adapter.
- ESP32 QEMU is not configured, so ESP32 simulation is unavailable even though upstream Velxio advertises it in its container/hosted builds.

## Release preparation and remote simulation

`RELEASE_STATUS.md` separates verified local features from public-deployment blockers. Request admission/body limits/security headers are mounted in the local API. Production mode requires explicit authentication settings, but shared-token auth is not a complete multi-user/project ownership system and browser login/WebSocket tickets are not finished. Compiler isolation is still required before accepting untrusted source publicly.

`deployment/local-production` contains a TLS/container reference. `deployment/remote-simulation` contains an authenticated, bounded ESP QEMU service and smoke script; configure its HTTPS endpoint and server-only token only after deployment. `backend/remote_projects.py` bridges project run/read/stop to that service. No remote server is provisioned here, Docker builds are unverified, and Linux Pi runtimes/guest images are not implemented. The genuine C3 browser adapter is experimental: the real Arduino firmware smoke test failed, so it is not advertised as working simulation.

## Beginner feasibility and permission

After requirements MCQs, the hardware agent must assess the full plan before placing any board or changing wires/firmware. `assess_project_feasibility` compares requested parts against actual catalog pins and implemented runtime behavior. The chat displays a **Before we build** card with plain-language reasons, choices to revise/cancel, and hardware-only continuation only when known pins permit it. Unknown joystick pins cannot be made valid through consent; unsupported servo motion is disclosed before work.

Reviews use server-issued project/revision-bound IDs and expire after 30 minutes. Mutations are restricted to reviewed operations, part types and quantities; changed projects require reassessment. Readback and limited circuit validation run before success, and known connection errors block compile/run. Final validation permits up to two correction passes; compiler retries permit at most two corrections. These are bounded checks, not complete electronics expertise or physical safety certification.

Paused assistant messages with no answer content are excluded from follow-up model history, avoiding invalid empty-message requests. Validation failures now show the exact request field instead of a generic 422. A live provider attempt at the new gate failed before assessment; deterministic service/agent/browser tests are separate from real provider success.

## Agent and project questions

The request-local LangGraph binds 16 real domain tools: project read, catalog search, component add/remove/modify, wire connect/remove, firmware generate/read/edit, compile, simulation run/stop/results, compiler diagnostics, calculator. `ask_project_questions` adds the project-understanding stage. `wire_circuit` adds atomic validated wire batches, `validate_circuit` reports limited known rail/LED topology diagnostics, and `search_example_requirements` consults all analyzed examples. Validation is not a general electrical-safety certification. Confirmation answers are persisted per conversation/project rather than shared between chats.

Initial project build prompts pause with a compact multiple-choice card. Confirming choices sends actual selected requirements to the same project's agent, which can mutate the circuit and firmware through the same services as manual controls. Read-before-mutation and revisions are enforced. Limits: 60 tool calls, 48 model calls, 300 seconds. Tool failures and provider failures are reported; the UI must not imply that advice alone built a project.

The activity feed streams actual tool starts/ends, provider reasoning where returned, answer text, and terminal status over NDJSON. Thinking is temporary and omitted from saved chat storage. Steps stay visible as a live checklist: a status header with elapsed time, one check-marked row per tool with narration kept inline, and each row expands to its actual command and output; final answers render below the checklist. It does not display a complete internal chain of thought, simulate shell commands, or invent tool executions. Chats are browser-local and project-scoped.

After actual project operations, the agent's final answer includes **Build it yourself** guidance: parts/values, exact wiring and polarity, power-off assembly, firmware/upload steps where applicable, expected behavior, checks, and troubleshooting. It must distinguish verified compilation/emulation from physical testing and explain incomplete work instead of inventing missing connections. This is part of the existing final LangGraph response, not an extra validation-model call.

## SPICE and instruments

Catalog includes DC voltage/current, sine/pulse/PWL/AC voltage sources, ground, resistors, capacitors, and inductors. DC Bias and Transient use actual ngspice WASM arrays and show real solver errors. AC sources carry small-signal parameters, but AC sweeps are not implemented.

Oscilloscope/logic analyzer uses bounded actual digital pin-edge capture. Channels, time/division, view pause/resume, and clear are available. Analog mode displays actual SPICE voltage samples or DC readings. Unknown/unobserved signals are not drawn as invented waveforms.

**Current boundaries:** The adapted peripheral runtime supports LED continuity and rail-connected buttons; other catalog peripherals may be placement-only. Analog DC/transient analysis is separate from MCU GPIO/ADC simulation, not a fully coupled mixed-signal engine. Pico uses UART0 (Serial1 compatibility), not emulated USB CDC or radio. The physical circuit view is 2D, not genuine 3D. Full upstream Velxio feature parity is not claimed.

## Source and license

Velxio source is preserved under `vendor/velxio`, pinned to revision `3e45cada3f362ce61fe2c8515f7fee0f7acf4113`. See `vendor/VELXIO_PROVENANCE.md`, upstream `LICENSE`, and `COMMERCIAL_LICENSE.md`. Velxio is AGPLv3/commercial dual-licensed; integrated distribution/network deployments must comply with the applicable license. Closed-source deployment requires appropriate licensing.

Reusable UI is in `src/components/ui`; `@/` maps to `src/`. Global styles are `src/index.css`; workspace and instrument styling is scoped in component CSS. Tailwind v4 and shadcn are configured.

## Checks

```powershell
npm run build
npm run lint
npm run test:api
npm test -- --workers=1
```

Tests distinguish injected model responses from actual compiler/emulator/ngspice execution. Browser tests use installed Chrome and software WebGL; real provider tests require available Groq quota. `npm run preview` serves frontend assets only, not the backend API.

The local API uses loopback guards and runtime ownership tokens. It is not an authenticated multi-user service. Add authentication, authorization, request limits, isolated compilation, and proper persistence before public deployment. Firmware compilation has no arbitrary shell tool, but native compilers still need deployment sandboxing.
