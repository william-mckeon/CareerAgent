# careeragent-frontend

> **The CareerAgent user interface** — the lean Streamlit chat UI that sits in front of `careeragent-api`.

---

## Overview

`careeragent-frontend` is the **user interface layer** of the CareerAgent system. It is a lean Streamlit web app that renders the chat experience, tracks in-session conversation state, and consumes a streaming response from [`careeragent-api`](../careeragent-api). That is the entire job.

This repo is scoped to the UI only. It has no model, no persona, no inference code, no database, no auth backend. It does not own the agent identity — the persona is owned upstream by `careeragent-api`. What lives here:

- The **Streamlit chat UI** that users talk to
- The **HTTP/SSE client** that streams responses from `careeragent-api`
- The **JSON ChatCompletion chunk decoder** (`sse_decoder.py`) that turns the byte stream into typed events
- The **multi-conversation sidebar** (`conversations.py`) — list, switch between, start, and delete conversations, all backed by `careeragent-sessions`
- The **conversation state** held per browser session and mirrored into `careeragent-sessions` so it survives a reload
- The **health gate** that locks the UI until the upstream model is ready
- The **error display** with the emoji prefixes (🔌 ⏳ 🔐 ⚠️ ❌)

I kept the boundary with `careeragent-api` deliberately sharp: the frontend owns *how the agent looks and feels to the user*, the gateway owns *who the agent is, how requests are authenticated, and how the upstream stream is relayed*. The two communicate over a stable HTTP/SSE contract.

---

## Where This Fits

```text
careeragent-os
│
├── careeragent-infra      ← separate repo
│   └── Model proxy → BYOC Provider (port 8002)
│       Stateless proxy that forwards to compute providers
│
├── careeragent-api        ← separate repo
│   └── FastAPI gateway (port 8001)
│       Owns the persona, auth chain, and SSE relay
│
├── careeragent-frontend   ← YOU ARE HERE
│   └── Streamlit chat UI (port 8000)
│       Pure UI layer. Talks only to careeragent-api.
│
└── careeragent-logger     ← separate repo
    └── Structured event log (called by careeragent-api)
```

The naming convention is intentional:

* `careeragent-infra` handles the **model** connectivity and compute provision.
* `careeragent-*` (api, frontend, logger) handle the **product** — gateway, UI, identity, and state.

**Port topology:**

```text
User → careeragent-frontend (:8000) → careeragent-api (:8001) → careeragent-infra (:8002) → BYOC Provider
```

Users only ever interact with port 8000. Port 8001 is `careeragent-api`, port 8002 is `careeragent-infra`, and the compute provider is reached over HTTPS — none of those layers are exposed to end users. `careeragent-frontend` is the only client of `careeragent-api`, and `careeragent-api` is the only thing `careeragent-frontend` talks to.

---

## Architecture

```text
┌─────────────────────────────────────────────────────────┐
│                    Browser (host)                       │
│                 http://localhost:8000                   │
└─────────────────────────────┬───────────────────────────┘
                              │
                              │ Host 8000 → Container 8501
                              ▼
┌─────────────────────────────────────────────────────────┐
│              Docker Container (careeragent-frontend)      │
│                                                         │
│  ┌─────────────────────────────────────────────────┐   │
│  │            Streamlit App                        │   │
│  │            src/frontend/app.py                  │   │
│  │                                                 │   │
│  │  • Chat UI (st.chat_message / st.chat_input)    │   │
│  │  • Reasoning effort toggle (selectbox)          │   │
│  │  • In-session state (session_id, messages)      │   │
│  │  • Health polling gate                          │   │
│  │  • Error display with emoji prefixes            │   │
│  │  • Reasoning expander vs main bubble routing    │   │
│  └──────────────────────┬──────────────────────────┘   │
│                         │ imports                      │
│  ┌──────────────────────▼──────────────────────────┐   │
│  │      SSE Decoder Module                         │   │
│  │      src/frontend/sse_decoder.py                │   │
│  │                                                 │   │
│  │  • Strips data: prefix                          │   │
│  │  • Detects [DONE] and [ERROR ...] sentinels     │   │
│  │  • JSON-decodes ChatCompletion chunks           │   │
│  │  • Routes by delta key:                         │   │
│  │      delta.reasoning → SSEEvent("reasoning")    │   │
│  │      delta.content   → SSEEvent("content")      │   │
│  │      finish_reason   → SSEEvent("finish")       │   │
│  │  • Yields typed SSEEvent objects to app.py      │   │
│  └─────────────────────────────────────────────────┘   │
└─────────────────────────────┬───────────────────────────┘
                              │
                              │ HTTP POST /chat (SSE stream)
                              │ HTTP GET  /health (readiness)
                              │ Header:   X-API-Key: CAREERAGENT_API_KEY
                              │ Target:   CAREERAGENT_API_URL
                              ▼
┌─────────────────────────────────────────────────────────┐
│        careeragent-api (SEPARATE REPO, SEPARATE STACK)    │
│                                                         │
│    FastAPI gateway, port 8001                           │
│    Owns: persona, auth chain,                           │
│          OpenAI messages list construction,             │
│          SSE relay, /health proxy                       │
└─────────────────────────────┬───────────────────────────┘
                              │
                              │ HTTP POST /chat (SSE stream)
                              │ Header:   X-API-Key: INFRA_API_KEY
                              │ Target:   CAREERAGENT_INFRA_URL
                              ▼
┌─────────────────────────────────────────────────────────┐
│      careeragent-infra (SEPARATE REPO, SEPARATE STACK)    │
│                                                         │
│    FastAPI proxy → BYOC Provider, port 8002             │
│    Stateless — full messages list sent every request    │
└─────────────────────────────┬───────────────────────────┘
                              │
                              │ HTTPS POST to Compute Provider
                              │ Header:   Authorization: Bearer PROVIDER_API_KEY
                              ▼
┌─────────────────────────────────────────────────────────┐
│      BYOC Compute Provider (e.g., RunPod, OpenAI)       │
│      base reasoning model                               │
│      nervous-system control layer                       │
└─────────────────────────────────────────────────────────┘
```

### Request flow

1. User types a message in the Streamlit chat input.
2. The frontend appends it to `st.session_state.messages`.
3. The frontend constructs a user/assistant-only messages list:

```json
[
  { "role": "user",      "content": "<first turn>" },
  { "role": "assistant", "content": "<first answer>" },
  { "role": "user",      "content": "<current input>" }
]
```

   **No system message.** `careeragent-api` prepends the persona server-side. If the frontend accidentally sends one, `careeragent-api` drops it with a warning log.
4. The list (plus optional `reasoning_effort`) is POSTed to `careeragent-api`'s `/chat` endpoint with the `X-API-Key: CAREERAGENT_API_KEY` header.
5. `careeragent-api` prepends the persona, validates auth, and forwards to `careeragent-infra`. `careeragent-infra` injects the reasoning level and forwards to the BYOC provider.
6. The chunks flow back through `careeragent-infra` and `careeragent-api` byte-for-byte (SSE relay) and arrive at the frontend.
7. `sse_decoder.py` consumes the raw line iterator from `requests.iter_lines()` and yields typed `SSEEvent` objects:
   * `kind="reasoning"` for chain-of-thought tokens (from `delta.reasoning`)
   * `kind="content"` for visible answer tokens (from `delta.content`)
   * `kind="finish"` for the `finish_reason` chunk
   * `kind="error"` for in-band `[ERROR ...]` sentinels
   * `kind="done"` for the `[DONE]` sentinel
8. `app.py`'s render loop routes each event to the right UI surface:
   * reasoning → live-streamed into a collapsible "Show thinking" expander
   * content → live-streamed into the main chat bubble
   * error → red banner via `st.error()`, loop breaks
   * done → loop breaks cleanly
9. When the stream completes, the assistant turn is appended to session state (without the reasoning — only `{role, content}` is persisted).

### Separation of concerns

| Concern | Lives in | Why |
| --- | --- | --- |
| Chat UI rendering | `careeragent-frontend` | Streamlit primitives, presentation only |
| In-session conversation state | `careeragent-frontend` | UI cache |
| Reasoning-format display | `careeragent-frontend` | UX decision (collapsible expander vs hidden vs inline) |
| Health gate (UI lock) | `careeragent-frontend` | Lock pattern lives where the UI is rendered |
| Error display | `careeragent-frontend` | Presentation; classification is upstream |
| Byte-level SSE / JSON parsing | `sse_decoder.py` | Isolated module so app.py doesn't import json |
| Persona / system prompt | `careeragent-api` | Backend identity concern, not a UI concern |
| Messages list construction | `careeragent-api` | Persona prepended server-side, single source of truth |
| Auth boundary to model layer | `careeragent-api` | Frontend holds one key; gateway holds the chain |
| Upstream error normalisation | `careeragent-api` | One classifier, not two; frontend trusts the codes |
| Model serving / inference | BYOC Provider | Heavy, GPU-dependent, handled by external compute |
| `Reasoning: <level>` injection | `careeragent-infra` | Single source of truth for the prompt format |

---

## Tech Stack

| Layer | Technology |
| --- | --- |
| Base image | `python:3.11-slim` |
| UI framework | Streamlit |
| HTTP client | `requests` (with `stream=True` for SSE) |
| Env loading | `python-dotenv` |
| Containerization | Docker + Docker Compose |
| Port (internal) | 8501 (Streamlit default) |
| Port (host) | 8000 |
| Auth | API key via `X-API-Key` header |
| Communication | HTTP/1.1 + Server-Sent Events |
| Backend dependency | `careeragent-api` (separate repo, port 8001) |

Intentionally absent: `torch`, `transformers`, `accelerate`, `fastapi`, `httpx`, `sqlalchemy`, etc. None of them belong in a UI layer. See `requirements.txt` for the rationale.

---

## Prerequisites

* **Docker Desktop** (macOS / Windows) or **Docker Engine + Compose v2** (Linux)
* **`careeragent-api` running and reachable** — either on the host, in another Docker container, or deployed elsewhere
* **Valid API key** — the same value set as `CAREERAGENT_API_KEY` in `careeragent-api`'s `.env`

You do **not** need:

* A GPU (no inference happens here, or anywhere in the local stack)
* Python installed on the host (Docker handles it) — unless you want to run locally for development
* Compute provider API keys (those belong to `careeragent-infra`)
* An `INFRA_API_KEY` value (that belongs to `careeragent-api`)

The frontend only needs `CAREERAGENT_API_KEY` — the secret for the frontend ↔ careeragent-api boundary. The other two boundary keys (`INFRA_API_KEY` for careeragent-api↔careeragent-infra, `PROVIDER_API_KEY` for careeragent-infra↔Provider) live in their respective services and never touch this repo.

---

## Project Structure

```text
careeragent-frontend/
├── docker/
│   └── frontend/
│       └── Dockerfile              # Python 3.11 slim + Streamlit
├── src/
│   └── frontend/
│       ├── app.py                  # The Streamlit UI
│       ├── sse_decoder.py          # SSE / ChatCompletion chunk decoder
│       └── conversations.py        # Conversation sidebar + state (careeragent-sessions)
├── docker-compose.yml              # Single-service compose
├── requirements.txt                # streamlit, requests, python-dotenv
├── .env                            # secrets — never commit this
├── .env.example                    # template for .env
├── .dockerignore                   # keeps .env and caches out of image
├── .gitignore                      # keeps .env and caches out of git
└── README.md                       # this file
```

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/william-mckeon/careeragent-frontend.git
cd careeragent-frontend
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Open `.env` and set the two required values:

```env
CAREERAGENT_API_URL=http://host.docker.internal:8001
CAREERAGENT_API_KEY=your_careeragent_api_key_here
```

**`CAREERAGENT_API_KEY` MUST match the `CAREERAGENT_API_KEY` value in `careeragent-api`'s `.env` exactly.** A mismatch produces `HTTP 401` on every `/chat` and `/health` call and surfaces in the UI as a 🔐 banner.

See [Configuration](#configuration) for the full list of supported variables and when to use which `CAREERAGENT_API_URL` value.

### 3. Make sure `careeragent-api` is running

`careeragent-frontend` is a thin client. Without `careeragent-api` reachable at `CAREERAGENT_API_URL`, the health gate sits on "🔌 Cannot reach careeragent-api" and the chat input never unlocks.

Start `careeragent-api` per its own README, then verify:

```bash
curl -H "X-API-Key: your_careeragent_api_key_here" http://localhost:8001/health
# {"status":"ok",...}           ← upstream warm, ready
# {"status":"loading",...}      ← compute worker spinning up
# {"status":"unreachable",...}  ← careeragent-api can't reach careeragent-infra
```

Note: `/health` is authenticated. Without the `X-API-Key` header you'll get a 401 even from a fully-running gateway.

### 4. Build and start

```bash
docker-compose up -d --build
```

The `--build` flag is **required** any time `app.py`, `sse_decoder.py`, or the Dockerfile changes. Without it Docker reuses the cached image.

First build takes 1–2 minutes (pip install layer). Subsequent builds are sub-second thanks to Docker layer caching.

### 5. Open in browser

```text
http://localhost:8000
```

On first load you will see one of four states:

* **🟢 "careeragent-api ready — starting chat"** — upstream is warm, chat input is live
* **⏳ "The upstream model is starting up"** — cold start; the page polls every 3 seconds and unlocks automatically when ready
* **🔌 "careeragent-api is up but cannot reach the upstream model"** — gateway is fine, careeragent-infra or compute provider is down
* **🔌 "Cannot reach careeragent-api at …"** — gateway unreachable; fix `CAREERAGENT_API_URL` in `.env` and retry

### 6. (Optional) Tail logs

```bash
docker-compose logs -f careeragent-frontend
```

Logs use the same format as `careeragent-api` and `careeragent-infra` so lines align when tailing all three services simultaneously. The frontend's named logger is `careeragent.frontend`, with a child `careeragent.frontend.sse_decoder` for the decoder module.

---

## How It Works

### The frontend owns presentation, not identity

The persona is owned by `careeragent-api` — the frontend has no copy, no path, and no system prompt file. It just sends user/assistant turns and trusts the gateway to do the right thing upstream.

This means:

* Any client — a mobile app, a CLI — gets the same agent identity by talking to `careeragent-api`, with no need to re-implement persona ownership
* A tampered or out-of-date frontend cannot override the persona
* The frontend stays out of the business of holding configuration that has no place at the UI layer

### Conversation history (client-side + careeragent-sessions)

`careeragent-api` is stateless across requests. Every `/chat` call must include the full history. `careeragent-frontend` holds the live history in `st.session_state.messages` and sends the complete list on every turn.

That in-tab list is no longer the *only* copy: the frontend routes through [`careeragent-sessions`](../careeragent-sessions), which persists each turn under a conversation id (returned in the `X-Conversation-Id` response header and mirrored into the URL as `?c=<id>`). This buys two things a pure-client-side history can't:

- **Reload-safe** — refreshing the page restores the open conversation from `careeragent-sessions` instead of starting blank.
- **Multiple conversations** — the sidebar (`conversations.py`) lists every saved conversation; clicking one loads its transcript, **➕ New conversation** starts a fresh one, and 🗑 deletes one. The whole switcher is a thin client over `careeragent-sessions`' `/conversations` CRUD — the frontend stores nothing itself.

The reasoning chain streams live during generation and is rendered in the expander, but it isn't stored back into history. The schema is clean OpenAI shape: `{"role", "content"}`.

### Health gate

During a serverless cold start `careeragent-infra` reports `{"status": "degraded"}`, which `careeragent-api` translates to `{"status": "loading"}` for the frontend.

To prevent users from firing messages that would just 503, `careeragent-frontend` implements a **blocking health gate**:

1. On every Streamlit rerun, if `session_state.model_ready` is `False`, a `while` loop polls `/health` every 3 seconds with the `X-API-Key` header.
2. A live status banner updates based on the response's top-level `status` field:
   * `ok` → flip `model_ready`, show 🟢 briefly, `st.rerun()` to load the chat UI
   * `loading` → show ⏳ with cold-start narrative and attempt counter
   * `unreachable` → show 🔌 with "careeragent-api is up but cannot reach the upstream model"
   * Connection error → show 🔌 with the URL and retry info
   * Anything else → show ⚠️ with the raw status
3. The chat UI is literally not rendered until the gate passes.

The frontend doesn't need to know about the compute provider or careeragent-infra; `careeragent-api` translates upstream vocabulary into a tidy three-value response.

### SSE streaming with JSON ChatCompletion chunks

The upstream emits OpenAI ChatCompletion chunks. Each event is JSON-encoded, with chain-of-thought tokens in `choices[0].delta.reasoning` and visible answer tokens in `choices[0].delta.content`. The two streams interleave only at chunk boundaries.

The decoding lives in `src/frontend/sse_decoder.py`:

1. Consumes raw lines from `response.iter_lines(decode_unicode=True)`
2. Skips blank lines and SSE comments silently
3. Strips the `data: ` prefix
4. Detects the two non-JSON sentinels (`[DONE]` and `[ERROR upstream_status=...]`) before attempting JSON parse
5. JSON-decodes everything else as a ChatCompletion chunk
6. Routes by `delta` key: `reasoning` → `SSEEvent("reasoning")`, `content` → `SSEEvent("content")`, `finish_reason` set → `SSEEvent("finish")`
7. Skips malformed chunks with a `WARNING` log
8. Yields typed `SSEEvent` dataclasses to `app.py`

`app.py`'s render loop then routes each event by `event.kind` — reasoning into the expander, content into the chat bubble, error into a red banner, done into a clean break.

### Error handling

The frontend exposes a consistent error taxonomy with emoji prefixes so I can scan logs and banners at a glance.

| Prefix | Class | Trigger |
| --- | --- | --- |
| 🔌 | Connection / network | TCP connect failed, `careeragent-api` unreachable, HTTP 502 |
| ⏳ | Timeout / model loading | Connect timeout, HTTP 503 (model loading), HTTP 504 (upstream timeout) |
| 🔐 | Auth | HTTP 401 — `CAREERAGENT_API_KEY` mismatch |
| ⚠️ | Request validation | HTTP 400 (empty messages) or HTTP 422 (invalid `reasoning_effort`) |
| ❌ | Unexpected | Anything else (parse errors, unexpected exceptions) |

Mid-stream errors arrive as in-band SSE events (`data: [ERROR upstream_status=503]`) which `sse_decoder.py` recognises and yields as `SSEEvent(kind="error", error=...)`. The render loop displays the banner and stops consuming.

On any error, the partial response is **not** appended to history.

---

## Configuration

All configuration is loaded from `.env` at the repository root via `python-dotenv` and `docker-compose`'s `env_file:` directive. See `.env.example` for the template.

| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `CAREERAGENT_API_URL` | string | `http://localhost:8001` | Base URL of `careeragent-api`. No trailing slash. |
| `CAREERAGENT_API_KEY` | string | — | Shared secret for `X-API-Key` header on `/chat` and `/health`. Required. |

### Choosing the right `CAREERAGENT_API_URL`

| Scenario | Value |
| --- | --- |
| Everything on host, no Docker | `http://localhost:8001` |
| Frontend in Docker, `careeragent-api` on host | `http://host.docker.internal:8001` |
| Both in Docker on a shared external network | `http://careeragent-api:8001` |
| External deployment | `https://api.your-domain.com` |

The compose file declares `extra_hosts: host.docker.internal:host-gateway` so `host.docker.internal` resolves correctly on Linux as well as Docker Desktop.

### The three-key compartmentalization model

`CAREERAGENT_API_KEY` in this file is **only** the frontend ↔ careeragent-api boundary key. It is not shared with the model layer. The full picture:

| Boundary | Key | Lives in |
| --- | --- | --- |
| frontend ↔ careeragent-api | `CAREERAGENT_API_KEY` | `careeragent-frontend/.env` + `careeragent-api/.env` |
| careeragent-api ↔ infra | `INFRA_API_KEY` | `careeragent-api/.env` + `careeragent-infra/.env` |
| infra ↔ Provider | `PROVIDER_API_KEY` | `careeragent-infra/.env` |

Each pair of services has its own shared secret. No key is forwarded unchanged through the chain.

### Generating a new API key

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Paste the output into **both** `careeragent-api/.env` (as `CAREERAGENT_API_KEY=…`) **and** `careeragent-frontend/.env` (as `CAREERAGENT_API_KEY=…`). The two must match byte-for-byte.

---

## Local Development (without Docker)

Sometimes you want faster iteration than a Docker rebuild. Run Streamlit directly on the host:

```bash
# 1. Create a virtualenv
python3.11 -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows

# 2. Install dependencies
pip install -r requirements.txt

# 3. Export env or rely on .env (python-dotenv picks it up)
export CAREERAGENT_API_URL=http://localhost:8001
export CAREERAGENT_API_KEY=your_careeragent_api_key_here

# 4. Run Streamlit on port 8000
streamlit run src/frontend/app.py \
  --server.port 8000 \
  --server.address 0.0.0.0 \
  --server.headless true \
  --server.fileWatcherType none
```

Streamlit supports hot-reload — edit `app.py` or `sse_decoder.py` and the browser refreshes automatically.

---

## Design Decisions

### Why Streamlit?

Streamlit collapses "build a chat UI, style it, add streaming, manage session state, serve it over HTTP" into a single Python file with no JavaScript. The HTTP/SSE contract with `careeragent-api` means the frontend can be swapped wholesale (mobile app, CLI, alternate web framework) without touching the backend later.

### Why is the SSE decoder a separate module?

1. **Single responsibility.** It isolates JSON-decoding-and-event-routing from Streamlit-and-rendering.
2. **Testability.** `parse_chunk()` takes a string and returns an event, unit-testable in isolation.
3. **Containment of upstream changes.** When the chunk format evolves, the change is contained to one file.

### Why a blocking health-polling loop?

Streamlit is single-threaded and lacks native auto-refresh. A blocking `while` loop with `st.empty()` status updates is the simplest correct pattern that keeps the page responsive (live status updates) and prevents users from firing requests that would 503.

### Why does the frontend trust careeragent-api's error normalisation?

Because `careeragent-api` owns the upstream relationship. Re-classifying errors at the frontend would mean duplicating logic that already exists upstream. `careeragent-api` maps upstream conditions onto a small set of HTTP status codes; the frontend just maps each code to an emoji prefix and a message.

### Why pure pass-through on `reasoning_effort`?

One source of truth. `careeragent-infra` holds the default; `careeragent-api` passes through; the frontend either sets a value or omits the field. Adding a frontend-side default would create two places to check when debugging.

### Why the emoji error-prefix scheme (🔌 ⏳ 🔐 ⚠️ ❌)?

Consistency. Once you've learned the error semantics in one part of the stack, you can read any log without a legend, and the prefixes are greppable.

### Why port 8501 internal and 8000 external?

8501 is Streamlit's default — keeping it as the container's internal port means zero Streamlit config overrides. 8000 is the user-facing port. The mapping happens in `docker-compose.yml` where it belongs.

### Why Python 3.11 slim?

Matches `careeragent-api`'s base image for consistency across the stack. Slim variant keeps the image around 450 MB total. No CUDA, no BLAS, no compilers needed — this is a pure-Python HTTP client.

---

## Known Limitations

These are present-tense limitations I'm aware of and have chosen to live with for now.

### No per-user identity

`CAREERAGENT_API_KEY` is a single shared secret, not a per-user credential. There is no concept of "user A vs user B" at this layer — anyone holding the key gets full access to whatever the frontend can do. Fine for trusted, single-operator use; not appropriate for public exposure without an auth layer in front.

### No rate limiting or abuse protection

The frontend trusts its users. If it were exposed to a wider audience, rate limiting would belong at `careeragent-api` or at a reverse proxy in front of it — not at the UI layer.

### Single shared identity (no per-user accounts)

The *live* conversation state lives in `st.session_state` (per browser tab), but conversations now persist in `careeragent-sessions`, so a reload restores the open one and the sidebar lists all of them — closing the tab no longer loses history. What is still missing is **per-user identity**: `CAREERAGENT_API_KEY` is one shared secret, so every browser sees the same shared pool of conversations. There is no "user A vs user B" separation at this layer.

### No context-window truncation

The frontend forwards whatever message list it has accumulated. A long enough conversation will eventually exceed the upstream model's context window and the request will fail upstream. There is no client-side summarisation or trimming.

### Cold-start UX is a blocking wait

During the initial serverless worker spin-up, users see a live status banner but cannot do anything else. Acceptable for a personal tool; a "come back later" UX would be more appropriate for a public-facing deployment. Nothing the frontend does can speed up the worker spin-up — the wait is at the inference layer.

---

## Troubleshooting

### 🔌 "Cannot reach careeragent-api at …"

The frontend cannot establish a TCP connection to `CAREERAGENT_API_URL`. Check:

1. Is `careeragent-api` running? `curl -H "X-API-Key: <your key>" http://localhost:8001/health` from the host.
2. Is `CAREERAGENT_API_URL` in `.env` correct for your topology?
3. On Linux, is `extra_hosts: host.docker.internal:host-gateway` doing its job? Try `docker exec careeragent-frontend getent hosts host.docker.internal`.

### 🔌 "careeragent-api is up but cannot reach the upstream model"

`careeragent-api` is reachable but its `/health` returned `{"status": "unreachable"}`. Debug at `careeragent-api` and `careeragent-infra`. Check:

1. Is `careeragent-infra` running? Is its URL in `careeragent-api/.env` correct?
2. Are provider credentials valid in `careeragent-infra/.env`?
3. Tail `careeragent-api` logs: `docker-compose -p careeragent-api logs -f`

### 🔐 "API key missing or invalid"

`CAREERAGENT_API_KEY` in `careeragent-frontend/.env` does not match `CAREERAGENT_API_KEY` in `careeragent-api/.env`. Copy the exact value across and restart both containers.

### ⏳ "The upstream model is starting up"

Normal on serverless cold starts. The serverless worker scales to zero when idle. The gate clears automatically once `careeragent-api` reports `status: ok`.

### "I see reasoning tokens in the main bubble"

If chain-of-thought text appears in the answer area instead of the expander, `sse_decoder.py` is misclassifying chunks. The upstream model may be putting CoT in `delta.content` directly.

### Changes to `app.py` or `sse_decoder.py` not taking effect

The Docker image bakes in source code at build time. Use `docker-compose up -d --build` to rebuild with the changes, OR run locally without Docker for hot-reload during iteration.

---

## License

Copyright © 2026 William McKeon.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

```text
http://www.apache.org/licenses/LICENSE-2.0
```

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

---

## Maintainer

**William McKeon** ([github.com/william-mckeon](https://github.com/william-mckeon))