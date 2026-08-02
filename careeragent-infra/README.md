# careeragent-infra

> **CareerAgent model inference infrastructure** — the model serving proxy layer of the CareerAgent system, backed by Amazon Bedrock.

---

## Overview

`careeragent-infra` is the inference proxy for CareerAgent. This repo is solely responsible for reaching the models on **Amazon Bedrock**, authenticating callers, and returning responses via a small, production-shaped REST API. It exposes two model routes — `POST /chat` (streamed chat, via Server-Sent Events) and `POST /embed` (embeddings, a single JSON response) — and is the single point through which every model in the system is reached.

It is intentionally scoped to the model layer only. It has no knowledge of the CareerAgent persona, the frontend, the conversation state, or the vector store — those live in separate repos. The boundary is clean by design: **careeragent-infra reaches the models, everything else builds on top of it.**

---

## Bedrock behind an OpenAI-shaped contract

`careeragent-infra` talks to Amazon Bedrock through **LiteLLM**, which normalizes Bedrock's request and response shapes to the OpenAI format on both ends. The rest of the stack only ever sees a stable, **OpenAI-shaped contract** — OpenAI messages in, OpenAI ChatCompletion SSE chunks / OpenAI embeddings JSON out. That is what let the backend move from an OpenAI-compatible HTTP endpoint to Bedrock without changing a single caller.

It routes between three logical models, each a Bedrock model id:

- **base_model** — the primary reasoning model handling everyday conversations (`/chat`, default). e.g. `bedrock/us.anthropic.claude-opus-4-8`.
- **nervous_system** — the fast, lightweight control layer for routing, history filtering, and agent decisions (`/chat`, `model="nervous_system"`). e.g. `bedrock/us.anthropic.claude-haiku-4-5`.
- **embedding model** — turns text into vectors for retrieval, e.g. resume and conversation search (`/embed`). e.g. `bedrock/amazon.titan-embed-text-v2:0`.

Swapping models or model sizes is a config change (a model id in `.env`), nothing more. Most current Claude models on Bedrock require a **cross-region inference profile** (the `us.` prefix) — confirm availability in your AWS Bedrock console first.

---

## Where This Fits

```text
careeragent-os
│
├── careeragent-infra      ← YOU ARE HERE
│   └── Model proxy API (port 8002)
│       Model proxy layer (→ Amazon Bedrock)
│
├── careeragent-frontend   ← separate repo
│   └── The product experience (port 8000)
│       Talks to careeragent-api
│
├── careeragent-api        ← separate repo
│   └── The Identity Gateway (port 8001)
│       Talks to careeragent-infra
│
└── careeragent-logger     ← separate repo
    └── The capture layer (port 8003)
```

The naming convention is intentional:
- `careeragent-infra` handles the **model** connectivity and compute provision.
- `careeragent-*` (api, frontend, logger) handle the **product** — gateway, UI, identity, and state.

**Port topology:**
```text
careeragent-api (:8001) → careeragent-infra (:8002) → Bedrock Base Model           [/chat, default]
                                                → Bedrock Control Layer Model  [/chat, model="nervous_system"]
                                                → Bedrock Embedding Model       [/embed]
```

`careeragent-api` is the primary caller of `careeragent-infra` today; other server-side services may call it as the system grows (for example, a retrieval layer embedding queries via `/embed`). The proxy is never called directly from a browser.

---

## Architecture

```text
┌───────────────────────────────────────────────────────┐
│                  Docker Container                     │
│                                                       │
│  ┌─────────────────────────────────────────────────┐  │
│  │          careeragent-infra  (port 8002)           │  │
│  │          FastAPI proxy — src/api/main.py        │  │
│  │                                                 │  │
│  │  POST /chat   →  validates X-API-Key            │  │
│  │               →  maps reasoning_effort → think  │  │
│  │               →  routes by model field          │  │
│  │               →  streams OpenAI-shaped SSE      │  │
│  │  POST /embed  →  validates X-API-Key            │  │
│  │               →  embeds input via Bedrock       │  │
│  │               →  returns JSON (no streaming)    │  │
│  │  GET  /health →  config + AWS-credential check  │  │
│  │  Auth: X-API-Key required on /chat and /embed   │  │
│  └────────┬─────────────┬──────────────┬───────────┘  │
└───────────┼─────────────┼──────────────┼──────────────┘
            │ /chat        │ /chat        │ /embed
            │ model="base" │ model=       │
            │ (default)    │ "nervous_..."│
            ▼              ▼              ▼
┌────────────────┐ ┌────────────────┐ ┌────────────────────┐
│ Amazon Bedrock │ │ Amazon Bedrock │ │ Amazon Bedrock     │
│ Base Model     │ │ Control Layer  │ │ Embedding Model    │
│ e.g. claude    │ │ e.g. claude    │ │ e.g. titan-embed   │
│  -opus         │ │  -haiku        │ │  -text-v2:0        │
│ reasoning model│ │ routing,       │ │ text → vectors     │
│ /chat default  │ │ history, ctrl  │ │ /embed             │
└────────────────┘ └────────────────┘ └────────────────────┘
        (reached via LiteLLM, authenticated with AWS credentials)
```

### Request flow — `/chat`

1. A caller sends `POST /chat` with `X-API-Key`, messages list, optional `reasoning_effort`, and optional `model`.
2. `careeragent-infra` validates the API key — returns `401` if missing or invalid.
3. It selects the Bedrock model id — base model by default, control layer when `model="nervous_system"` — and returns a real `503` pre-flight if that route is unconfigured.
4. It calls Bedrock via LiteLLM with the messages, `reasoning_effort` mapped to Claude's extended-thinking depth, and the AWS credential chain.
5. LiteLLM normalizes Bedrock's streamed output to OpenAI ChatCompletion chunks; the proxy re-emits each as an SSE event (thinking on `delta.reasoning`, answer on `delta.content`).
6. A final `data: [DONE]` event signals end of stream. (A Bedrock failure mid-stream surfaces as a `data: [ERROR] ...` event followed by `[DONE]`, since the response is already `HTTP 200`.)

### Request flow — `/embed`

1. A caller sends `POST /embed` with `X-API-Key` and `input` (a string or list of strings).
2. `careeragent-infra` validates the API key and that `input` is non-empty.
3. If `EMBEDDING_MODEL` is unset, it returns `503` ("not configured"); `/chat` is unaffected.
4. Otherwise it embeds the input via Bedrock through LiteLLM and returns the OpenAI-shaped embeddings JSON (no streaming).

### System prompt ownership

The system prompt — the persona — is owned upstream by **careeragent-api**. `careeragent-api` sends it as the first message in the OpenAI messages list on every `/chat` request. `careeragent-infra` forwards the messages to Bedrock unmodified; the reasoning effort is applied as a Bedrock thinking-depth parameter, not as message text. `careeragent-infra` never stores or inspects the system prompt content. The `/embed` route carries no persona — it forwards raw input only.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Base image | `python:3.12-slim` |
| Model serving | Amazon Bedrock (via LiteLLM) |
| API proxy | FastAPI + uvicorn |
| Streaming | SSE (`/chat`); single JSON response (`/embed`) |
| Auth | `X-API-Key` header (caller) + AWS credentials (Bedrock) |
| Containerization | Docker + Docker Compose |

---

## Prerequisites

- **Docker Desktop** installed.
- **An AWS account with Amazon Bedrock model access** for the models you configure (base, and optionally a nervous-system control model and an embedding model). Most current Claude models require a cross-region inference profile.
- **AWS credentials** — static keys (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`) or an attached IAM role / shared profile, with `bedrock:InvokeModel` + `bedrock:InvokeModelWithResponseStream` permissions — plus `AWS_REGION_NAME`.
- **API_KEY** — a secret key shared with `careeragent-api` (and any other server-side caller) for request authentication.

No local GPU required — all inference runs on Bedrock.

---

## Project Structure

```text
careeragent-infra/
├── docker/
│   └── model/
│       └── Dockerfile              # python:3.12-slim — proxy only, no CUDA
├── src/
│   └── api/
│       └── main.py                 # FastAPI proxy — auth, routing, Bedrock via LiteLLM, embeddings
├── docker-compose.yml
├── requirements.txt
├── .env                            # secrets — never commit this
├── .env.example                    # template for .env
├── .dockerignore
├── .gitignore
└── README.md
```

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/william-mckeon/careeragent-infra.git
cd careeragent-infra
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Edit `.env` and fill in your values. The model variables are **LiteLLM Bedrock model ids** for models enabled in your account + region:

```env
API_KEY=your_long_random_secret_key_here

# AWS / Bedrock auth
AWS_REGION_NAME=us-east-1
AWS_ACCESS_KEY_ID=your_aws_access_key_id
AWS_SECRET_ACCESS_KEY=your_aws_secret_access_key

# Bedrock model ids
BASE_MODEL=bedrock/us.anthropic.claude-opus-4-8
NERVOUS_SYSTEM_MODEL=bedrock/us.anthropic.claude-haiku-4-5
EMBEDDING_MODEL=bedrock/amazon.titan-embed-text-v2:0

REASONING_EFFORT=medium
MAX_TOKENS=8192
REQUEST_TIMEOUT=600
```

`NERVOUS_SYSTEM_MODEL` and `EMBEDDING_MODEL` are optional — if unset, those routes report "not configured" and the base `/chat` path is unaffected. If you use an attached IAM role or a shared AWS profile, you can omit the static `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` and set only `AWS_REGION_NAME`. A Bedrock long-term API key works too — set `AWS_BEARER_TOKEN_BEDROCK` instead of the key/secret pair.

The proxy retries transient Bedrock errors (503 bursts / 429 throttles) with exponential backoff (`MODEL_RETRIES`, `BACKOFF_CAP`) and fails fast on permanent ones — a robustness lesson carried over from a prior Bedrock migration, where serverless Bedrock's 503 bursts on large requests outlasted a flat retry cap.

> **Embedding dimension** must match the pgvector column in careeragent-memory. Titan Text Embeddings v2 emits 1024-dim vectors.

Generate a secure `API_KEY` with:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### 3. Build the image

```bash
docker-compose build --no-cache
```

### 4. Start the API proxy

```bash
docker-compose up -d
```

The API is ready when you see:

```text
=== CareerAgent Inference API Ready — listening on :8002 ===
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8002
```

Startup takes under 10 seconds — the proxy has no model to load.

---

## API Reference

### `POST /chat`

Send a full OpenAI messages list and receive a streamed response via Server-Sent Events. Optionally set the reasoning effort level and model per request.

Requires a valid `X-API-Key` header on every request.

**Request headers:**
```text
Content-Type: application/json
X-API-Key: your_api_key_here
```

**Request body:**
```json
{
  "messages": [
    {"role": "system",    "content": "You are CareerAgent..."},
    {"role": "user",      "content": "hello"}
  ],
  "reasoning_effort": "medium",
  "model": "base"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `messages` | array | Yes | Full OpenAI messages list including system prompt |
| `reasoning_effort` | string | No | `low`, `medium`, or `high`. Defaults to `medium`. Maps to Claude's extended-thinking depth. |
| `model` | string | No | `base` (default) or `nervous_system`. Routes to the base model or the control-layer model. |

**Error responses:**
- `400` — messages list is empty (`{"detail": "Messages list cannot be empty"}`)
- `400` — messages contain no `user`-role message (`{"detail": "Messages must include at least one user message"}`)
- `401` — API key missing or invalid
- `422` — request body malformed
- `503` — the selected `model` route is unconfigured, e.g. `model="nervous_system"` with `NERVOUS_SYSTEM_MODEL` unset (`{"detail": "<model> model is not configured"}`); returned pre-flight, before the stream begins

Bedrock-side failures do **not** surface as an HTTP error: once the stream begins the response is already `HTTP 200`, so a Bedrock error (throttle, access-denied, unavailable model) is reported as an in-stream `data: [ERROR] ...` event followed by `data: [DONE]`. Watch the stream, not just the status code.

**curl:**
```bash
curl -X POST http://localhost:8002/chat \
     -H "Content-Type: application/json" \
     -H "X-API-Key: your_api_key_here" \
     -d '{"messages": [{"role": "system", "content": "You are CareerAgent..."}, {"role": "user", "content": "hello"}], "reasoning_effort": "medium"}' \
     --no-buffer
```

---

### `POST /embed`

Send one or more strings and receive an OpenAI-compatible embeddings JSON as a single response (no streaming). Used to turn text into vectors — e.g. embedding resume sections or conversation turns for storage in a vector database, and embedding a query at retrieval time.

Requires a valid `X-API-Key` header on every request.

**Request headers:**
```text
Content-Type: application/json
X-API-Key: your_api_key_here
```

**Request body** (single string, or a list to batch):
```json
{ "input": ["first chunk", "second chunk"] }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `input` | string or array of strings | Yes | Text to embed. A list is embedded in one batched call. Cannot be empty. |

The caller sends no `model` field (the route is selected by the `EMBEDDING_MODEL` config) and no `reasoning_effort` (the embedding model does not reason).

**Response:** OpenAI-compatible embeddings JSON:
```json
{
  "object": "list",
  "data": [ { "object": "embedding", "index": 0, "embedding": [0.0123, -0.0456, "..."] } ],
  "model": "<bedrock model id>",
  "usage": { "prompt_tokens": 7, "total_tokens": 7 }
}
```

**Error responses:**
- `400` — `input` is empty
- `401` — API key missing or invalid
- `422` — request body malformed
- `502` — Bedrock returned an error embedding the input, or a proxy error
- `503` — `EMBEDDING_MODEL` not set

**curl:**
```bash
curl -X POST http://localhost:8002/embed \
     -H "Content-Type: application/json" \
     -H "X-API-Key: your_api_key_here" \
     -d '{"input": ["first chunk", "second chunk"]}'
```

---

### `GET /health`

Health check. No authentication required. A local configuration + AWS-credential-presence check — it makes **no** Bedrock call.

**Fully ready:**
```json
{"status": "ok", "proxy": "ok", "base_model": "ok", "nervous_system": "ok", "embedding": "ok"}
```

**AWS credentials / region missing:**
```json
{"status": "degraded", "proxy": "ok", "base_model": "unreachable", "nervous_system": "unreachable", "embedding": "unreachable"}
```

**Nervous-system / embedding not configured:**
```json
{"status": "ok", "proxy": "ok", "base_model": "ok", "nervous_system": "not configured", "embedding": "not configured"}
```

A route is `ok` when its model id is set **and** AWS credentials + region resolve; `unreachable` when configured but credentials/region are missing; `not configured` when its model id is unset. `status` is `ok` when the base route is `ok`, `degraded` otherwise. Because `/health` does not call Bedrock, actual per-model access errors (no IAM access, throttling) surface at `/chat` / `/embed` call time, not here.

```bash
curl http://localhost:8002/health
```

---

### `GET /docs`

Auto-generated Swagger UI. **Disabled by default; set `INFRA_ENABLE_DOCS=true` to enable.** When disabled, `/docs`, `/redoc`, and `/openapi.json` are not mounted (the proxy is an internal, server-to-server service).

```text
http://localhost:8002/docs
```

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | — | Secret key for X-API-Key auth on `/chat` and `/embed` (required) |
| `AWS_REGION_NAME` | — | AWS region for Bedrock, e.g. `us-east-1`. Falls back to `AWS_REGION` (required) |
| `AWS_ACCESS_KEY_ID` | — | AWS access key. Optional if an IAM role / profile provides credentials |
| `AWS_SECRET_ACCESS_KEY` | — | AWS secret key. Same handling as above |
| `AWS_SESSION_TOKEN` | — | Only for temporary (STS) credentials |
| `BASE_MODEL` | — | LiteLLM Bedrock model id for the base model. Default for all `/chat` requests (required) |
| `NERVOUS_SYSTEM_MODEL` | — | LiteLLM Bedrock model id for the fast control model. Used when `model="nervous_system"`. Optional |
| `EMBEDDING_MODEL` | — | LiteLLM Bedrock model id for the embedding model. Used by `POST /embed`. Optional |
| `AWS_BEARER_TOKEN_BEDROCK` | — | Alternative auth: a Bedrock long-term API key (single bearer token). If set, leave the access-key/secret pair blank |
| `REASONING_EFFORT` | `medium` | Default reasoning level for the chat models — `low`, `medium`, or `high` |
| `MAX_TOKENS` | `8192` | Max output tokens per `/chat` generation |
| `REQUEST_TIMEOUT` | `600` | Per-request timeout (seconds) for Bedrock calls |
| `MODEL_RETRIES` | `5` | Retries for transient Bedrock 503/429 errors (`/chat` retries before the first chunk only; `/embed` fully). `0` disables |
| `BACKOFF_CAP` | `20` | Max seconds for one retry's exponential backoff (jitter added on top) |

---

## Design Decisions

### Why keep the FastAPI proxy layer instead of calling Bedrock directly?

Putting caller authentication in one place keeps the AWS credentials out of the gateway and frontend layers — only this service holds them. It also gives a stable, **OpenAI-shaped** contract regardless of what sits behind it: the backend moved from an OpenAI-compatible HTTP endpoint to Amazon Bedrock without any caller change. It is the single chokepoint every model path runs through — chat and embeddings alike.

### Why LiteLLM?

Bedrock does not speak the OpenAI wire format natively. LiteLLM normalizes Bedrock to the OpenAI shape on both ends, so the proxy can present the exact same `/chat` SSE and `/embed` JSON contract the rest of the stack already depends on, with far less code than calling `bedrock-runtime` directly.

### Why a separate `/embed` endpoint instead of a `model` route on `/chat`?

`/chat` is welded to the messages-in / SSE-stream-out contract. Embeddings have a different request shape (raw input strings), a different response (a single JSON vector array, no streaming), and no reasoning. They get their own endpoint while reusing the same auth and the same Bedrock credentials. (Because `/embed` is non-streaming, it returns real HTTP error codes; `/chat` can only report failures as an in-stream `[ERROR]` event, since its `200` is already committed.)

### Why a caller key and AWS credentials?

`API_KEY` is the key a caller sends to `careeragent-infra` — it authenticates the caller. The AWS credentials authenticate `careeragent-infra` to Bedrock. These concerns are deliberately separated so the caller key can be rotated without affecting AWS configuration, and the AWS credentials never leave this service. The caller-side validation is isolated so it can later move from a single shared key to per-caller keys.

### Why reasoning effort as an API parameter?

Both chat models support configurable reasoning depth — low, medium, high. It's a genuine feature: a frontend can expose it as Quick / Standard / Deep mode, and tooling can set it per use case. The proxy maps it to Claude's extended-thinking depth on Bedrock automatically.

### Why OpenAI messages / embeddings format?

The OpenAI formats make `careeragent-infra` compatible with almost any frontend, keep the serving layer stateless and the protocol standard — and are exactly what let the backend swap to Bedrock without touching a single caller.

### Why port 8002?

Port 8000 is reserved for careeragent-frontend. Port 8001 is reserved for careeragent-api. Port 8002 is the exposed port for careeragent-infra.

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
