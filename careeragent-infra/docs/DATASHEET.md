# careeragent-infra — Datasheet

> Reference document for building on top of careeragent-infra.
> Intended audience: **careeragent-api** (the primary caller today) and any
> other server-side service that needs to understand what careeragent-infra is,
> what it owns, and how it is called.

---

## Quick Reference

| Item | Value |
|---|---|
| Role | Model inference proxy for the CareerAgent system |
| Backend | **Amazon Bedrock** (via LiteLLM) |
| Base URL | `http://localhost:8002` |
| Protocol | HTTP/1.1 |
| Streaming | SSE (`/chat`) · single JSON response (`/embed`) |
| Auth in (caller) | `X-API-Key` header (required on `/chat` and `/embed`) |
| Auth out (Bedrock) | AWS credentials (env keys, profile, or attached IAM role) + region |
| Content type in | `application/json` |
| Content type out | `text/event-stream` (`/chat`) · `application/json` (`/embed`) |
| Request format | OpenAI messages (`/chat`) · OpenAI embeddings input (`/embed`) |
| Response format | OpenAI ChatCompletion chunks (`/chat`) · OpenAI embeddings JSON (`/embed`) |
| Reasoning effort | `low` / `medium` / `high` (optional `/chat` field, default: `medium`) |
| Model selection | `base` (default) / `nervous_system` (optional `/chat` field) |
| Chat endpoint | `POST /chat` |
| Embed endpoint | `POST /embed` |
| Health endpoint | `GET /health` |
| Docs UI | `GET /docs` (disabled by default; set `INFRA_ENABLE_DOCS=true` to enable) |
| Primary caller | careeragent-api (other server-side callers possible as the system grows) |
| Version | 2.0.0 |

---

## Overview

`careeragent-infra` is the **model inference proxy** of the CareerAgent system. It sits between its server-side callers (careeragent-api today, and potentially other internal services as the system grows) and **Amazon Bedrock**. It is the single point through which every model in the system is reached — chat models and the embedding model alike.

It does two jobs:

- **Chat (`POST /chat`)** — authenticates the caller, maps the per-request reasoning effort to Claude's extended-thinking depth, routes to the base or nervous-system model on Bedrock, and streams the response back as Server-Sent Events.
- **Embeddings (`POST /embed`)** — authenticates the caller, embeds the input via the Bedrock embedding model, and returns an OpenAI-compatible embeddings JSON as a single response (no streaming).

It speaks to Bedrock through **LiteLLM**, which normalizes Bedrock's request and response shapes to the OpenAI format on both ends. That is the whole point of this layer: the rest of the stack only ever sees a stable, **OpenAI-shaped contract**, so the model backend can change (it was an OpenAI-compatible HTTP endpoint before; it is Amazon Bedrock now) without any caller having to know or change.

It is intentionally scoped to the model layer only. It has no knowledge of the CareerAgent persona, the frontend, the conversation state, the vector store, or the capture layer. The boundary is clean by design: **careeragent-infra reaches the models; everything else builds on top of it.**

---

## Where This Service Fits

```text
┌──────────────────────────────────────────────────────────────┐
│    Callers  (server-side, separate repos / Docker stacks)    │
│                                                              │
│    careeragent-api on :8001 is the primary caller today. Other │
│    internal services may call the proxy as the system grows  │
│    (e.g. a retrieval layer embedding queries via /embed).    │
│    api owns the persona, the auth chain, and the SSE relay,  │
│    and constructs the full messages list (system prompt      │
│    first) for /chat.                                         │
└───────────────────────────┬──────────────────────────────────┘
              │ POST /chat   (SSE response)    X-API-Key: <API_KEY>
              │ POST /embed  (JSON response)   X-API-Key: <API_KEY>
              │ GET  /health (no auth)
              ▼
┌──────────────────────────────────────────────────────────────┐
│    careeragent-infra   ←── YOU ARE READING THIS DATASHEET      │
│    FastAPI proxy on :8002                                    │
│                                                              │
│    Owns: caller auth, reasoning-effort → thinking-depth       │
│          mapping (chat), model routing (base /               │
│          nervous_system / embedding), the AWS credentials,   │
│          OpenAI-shape translation in and out (LiteLLM)       │
│    Stateless — the full request is sent on every call        │
└──────┬───────────────────────┬───────────────────────┬───────┘
       │ /chat model="base"     │ /chat                  │ /embed
       │ (default)              │ model="nervous_system" │
       ▼                        ▼                        ▼
┌────────────────────┐ ┌──────────────────────┐ ┌────────────────────┐
│ Amazon Bedrock     │ │ Amazon Bedrock       │ │ Amazon Bedrock     │
│ Base Model         │ │ Control Layer        │ │ Embedding Model    │
│ (BASE_MODEL)       │ │ (NERVOUS_SYSTEM_     │ │ (EMBEDDING_MODEL)  │
│ reasoning model    │ │  MODEL)              │ │ text → vectors     │
│ all /chat default  │ │ fast control model:  │ │ used by /embed     │
│                    │ │ routing, history     │ │                    │
│ e.g. claude-opus   │ │ filtering, decisions │ │ e.g. titan-embed   │
│                    │ │ e.g. claude-haiku    │ │  -text-v2:0        │
└────────────────────┘ └──────────────────────┘ └────────────────────┘
   Bedrock Converse        Bedrock Converse          Bedrock Embeddings
   (streamed)              (streamed)                (single response)
```

**Port topology:**
```text
careeragent-api (:8001) → careeragent-infra (:8002) → Bedrock Base Model           [/chat, default]
                                                 → Bedrock Control Layer Model  [/chat, model="nervous_system"]
                                                 → Bedrock Embedding Model      [/embed]
```

`careeragent-infra` is reached only by server-side callers — never directly from a browser. It never sees `CAREERAGENT_API_KEY` (the frontend↔api secret) or any conversation-capture data. Every model path in the system runs through it; nothing talks to Bedrock directly.

---

## Authentication

There are two independent boundaries, each with its own credential.

**Inbound — caller → infra (`X-API-Key`).** Every `POST /chat` and `POST /embed` request must include a valid API key in the `X-API-Key` header.

```text
X-API-Key: your_api_key_here
```

Requests with a missing or invalid key receive `401 Unauthorized`. The `/health` endpoint does not require authentication. The API key is a shared secret between `careeragent-infra` and its caller, set via `API_KEY` in `careeragent-infra`'s `.env`. **It is the same value `careeragent-api` holds as `INFRA_API_KEY`** — the naming differs across the boundary but the value must match byte-for-byte. If more than one server-side service calls the proxy, they share this key today; per-caller keys are a future evolution (the validation is isolated in `verify_api_key` precisely so it can move from a static key to a per-caller lookup without changing the endpoint contract).

**Outbound — infra → Bedrock (AWS credentials).** `careeragent-infra` authenticates to Amazon Bedrock with **AWS credentials**, resolved by LiteLLM/boto3 from the standard chain: `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (+ optional `AWS_SESSION_TOKEN`) from the environment, a shared AWS profile, or an attached IAM role — plus `AWS_REGION_NAME`. Alternatively, set `AWS_BEARER_TOKEN_BEDROCK` to a Bedrock long-term API key (a single bearer token, no key/secret pair to rotate); `AWS_REGION_NAME` is still required. These credentials are never exposed to any caller. The IAM principal needs Bedrock model-invocation permissions (`bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream`) for the configured model ids.

Two independent secrets at two independent boundaries: `API_KEY` gates who may call `careeragent-infra`; the AWS credentials authenticate `careeragent-infra` to Bedrock. Either can be rotated without touching the other.

---

## API Reference

### `POST /chat`

The chat inference endpoint. Send a full OpenAI messages list and receive a token-by-token streamed response via SSE. Optionally control the reasoning effort level and select the model per request.

#### Request

```text
POST /chat
Content-Type: application/json
X-API-Key: your_api_key_here
```

```json
{
  "messages": [
    {"role": "system",    "content": "<persona, prepended by careeragent-api>"},
    {"role": "user",      "content": "hello"}
  ],
  "reasoning_effort": "medium"
}
```

**With reasoning_effort set to high:**
```json
{
  "messages": [
    {"role": "system", "content": "<persona>"},
    {"role": "user",   "content": "Analyze the tradeoffs between SSE and WebSockets for a streaming chat application"}
  ],
  "reasoning_effort": "high"
}
```

**Multi-turn example:**
```json
{
  "messages": [
    {"role": "system",    "content": "<persona>"},
    {"role": "user",      "content": "What is the Fibonacci sequence?"},
    {"role": "assistant", "content": "The Fibonacci sequence is..."},
    {"role": "user",      "content": "Can you show me in Python?"}
  ],
  "reasoning_effort": "medium"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `messages` | array | Yes | Full OpenAI messages list. Cannot be empty. Must contain at least one `user` message. |
| `messages[].role` | string | Yes | One of `system`, `user`, `assistant`. |
| `messages[].content` | string | Yes | The message content. |
| `reasoning_effort` | string | No | `low`, `medium`, or `high`. Defaults to the server `REASONING_EFFORT` env var (medium). |
| `model` | string | No | `base` (default) or `nervous_system`. Routes to the base reasoning model or the control-layer model. Omitting the field always routes to the base model. |

**Important:** the caller is responsible for constructing the full messages list, including the persona as the system message. `careeragent-infra` forwards the messages to Bedrock unmodified — it does **not** add or rewrite any message. The `reasoning_effort` field is mapped to Claude's extended-thinking depth on the Bedrock call (it is not injected into the message text). This applies to both the base model and the control-layer model.

#### Reasoning effort guidance

| Level | Latency | Use for |
|---|---|---|
| `low` | Fastest | Lightweight tooling calls, simple lookups, routing decisions |
| `medium` | Balanced | Standard interactions, general questions (default) |
| `high` | Slowest | Complex analysis, multi-step reasoning, hard problems |

#### Response

```text
HTTP/1.1 200 OK
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache
Transfer-Encoding: chunked
X-Accel-Buffering: no
```

Each SSE event payload is a JSON-encoded OpenAI ChatCompletion chunk — NOT plain text tokens. Chain-of-thought (Claude's extended thinking) streams first inside `choices[0].delta.reasoning`, then visible answer tokens inside `choices[0].delta.content`, then a final empty-delta chunk with `finish_reason: "stop"`, then the `[DONE]` sentinel.

```text
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"reasoning":"User"},"finish_reason":null}]}

...  (more reasoning tokens — chain-of-thought)

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}

...  (more content tokens — visible answer)

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

`careeragent-infra` **translates** the Bedrock stream into this OpenAI-shaped SSE: LiteLLM normalizes Bedrock's streaming events to OpenAI chunks, and the proxy re-emits each as a `data: {json}` event, mapping Claude's thinking tokens onto `delta.reasoning` and answer tokens onto `delta.content`. The wire format the caller receives is identical to the previous OpenAI-compatible backend, terminating with the `[DONE]` sentinel. (This is the one place the proxy is no longer a pure byte relay — it cannot be, because Bedrock does not speak OpenAI SSE natively — but the *output contract* is unchanged.)

**Important:** The stream always ends with `data: [DONE]`. The caller must watch for this event to know generation is complete.

**Important:** Both the base model and the control-layer model are reasoning models — each emits a thinking chain in `delta.reasoning` before the final answer in `delta.content`. Display or filter the reasoning per the caller's UX choice; see the SSE Stream Specification section below.

#### Error responses

| Status | Condition | Body |
|---|---|---|
| `400` | `messages` list is empty | `{"detail": "Messages list cannot be empty"}` |
| `400` | `messages` contains no `user`-role message | `{"detail": "Messages must include at least one user message"}` |
| `401` | X-API-Key header missing or invalid | `{"detail": "Invalid or missing API key"}` |
| `422` | Request body malformed or missing | FastAPI validation error JSON |
| `503` | Selected `model` route is unconfigured (e.g. `model="nervous_system"` with `NERVOUS_SYSTEM_MODEL` unset) — returned pre-flight, before the stream begins | `{"detail": "<model> model is not configured"}` |

**Bedrock-side failures on `/chat` are not HTTP errors.** Once the SSE stream has begun the response is already `HTTP 200`, so a Bedrock error (throttling, access-denied, an unavailable model) cannot be reported as an HTTP status. Instead it surfaces as an in-stream event — `data: [ERROR] internal proxy error` followed by `data: [DONE]`. The caller must watch the stream for an `[ERROR]` payload, not only the HTTP status. (The `/embed` endpoint, being a single non-streaming response, *does* return real error status codes — see below.)

#### Example — curl (testing only)

```bash
curl -X POST http://localhost:8002/chat \
     -H "Content-Type: application/json" \
     -H "X-API-Key: your_api_key_here" \
     -d '{"messages": [{"role": "system", "content": "..."}, {"role": "user", "content": "hello"}], "reasoning_effort": "medium"}' \
     --no-buffer
```

---

### `POST /embed`

The embedding endpoint. Send one or more strings and receive an OpenAI-compatible embeddings response as a single JSON body — no streaming. Used to turn text into vectors: for example, embedding resume sections or conversation turns before storing them in a vector database, and embedding a query at retrieval time.

#### Request

```text
POST /embed
Content-Type: application/json
X-API-Key: your_api_key_here
```

**Single string:**
```json
{ "input": "the quick brown fox" }
```

**Batch (preferred when embedding several items at once):**
```json
{ "input": ["first chunk", "second chunk", "third chunk"] }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `input` | string or array of strings | Yes | Text to embed. A list is embedded in a single batched call. Cannot be empty. |

The caller sends no `model` field — the embedding route is selected by the server-side `EMBEDDING_MODEL` config, the same way `/chat` selects a model by the `base` / `nervous_system` route rather than naming a Bedrock model in the body. There is no `reasoning_effort` — the embedding model does not reason.

#### Response

```text
HTTP/1.1 200 OK
Content-Type: application/json
```

An OpenAI-compatible embeddings JSON:

```json
{
  "object": "list",
  "data": [
    { "object": "embedding", "index": 0, "embedding": [0.0123, -0.0456, "..."] }
  ],
  "model": "<bedrock model id>",
  "usage": { "prompt_tokens": 7, "total_tokens": 7 }
}
```

For a batch input, `data` contains one entry per input string, each tagged with its `index`. The vector dimensionality is whatever the embedding model emits (e.g. 1024 for Titan Text Embeddings v2) — `careeragent-infra` does not pin or transform it. **The dimension must match the pgvector column in careeragent-memory.**

#### Error responses

| Status | Condition | Body |
|---|---|---|
| `400` | `input` is empty (empty string or empty list) | `{"detail": "Input cannot be empty"}` |
| `401` | X-API-Key header missing or invalid | `{"detail": "Invalid or missing API key"}` |
| `422` | Request body malformed (e.g. `input` missing or wrong type) | FastAPI validation error JSON |
| `502` | Bedrock returned an error embedding the input, or an unexpected proxy error | `{"detail": "Embedding provider error"}` |
| `503` | `EMBEDDING_MODEL` not set | `{"detail": "Embedding model not configured"}` |

Unlike `/chat`, `/embed` is a single JSON response, so Bedrock failures are returned as real HTTP error status codes rather than in-stream events.

#### Example — curl (testing only)

```bash
curl -X POST http://localhost:8002/embed \
     -H "Content-Type: application/json" \
     -H "X-API-Key: your_api_key_here" \
     -d '{"input": ["first chunk", "second chunk"]}'
```

---

### `GET /health`

Lightweight health check. No authentication required. Reports the proxy plus the three model routes.

#### Request

```text
GET /health
```

#### Response — fully ready

```json
{"status": "ok", "proxy": "ok", "base_model": "ok", "nervous_system": "ok", "embedding": "ok"}
```

#### Response — AWS credentials / region missing

```json
{"status": "degraded", "proxy": "ok", "base_model": "unreachable", "nervous_system": "unreachable", "embedding": "unreachable"}
```

#### Response — nervous-system / embedding not configured

```json
{"status": "ok", "proxy": "ok", "base_model": "ok", "nervous_system": "not configured", "embedding": "not configured"}
```

**Note:** Always returns HTTP `200`. With the Bedrock backend, "reachable" means **configured and credentialed**: Bedrock is a managed, always-on endpoint, so there is no host to ping and no cold/scale-to-zero worker to distinguish. A route is:
- `ok` — its model id is set **and** AWS credentials + region resolve.
- `unreachable` — its model id is set but credentials/region are missing.
- `not configured` — its model id is unset in `.env`.

`status` is `ok` when the base route is `ok`, and `degraded` otherwise. `nervous_system` and `embedding` are reported independently and do **not** affect the top-level `status` (either may be unconfigured). `/health` performs **no** Bedrock network call — it is a local configuration + credential-presence check, so actual per-model access errors (a model you lack access to, a throttle) surface at `/chat` / `/embed` call time, not here.

```bash
curl http://localhost:8002/health
```

---

### `GET /docs`

Auto-generated Swagger UI. For interactive testing only. **Disabled by default; set `INFRA_ENABLE_DOCS=true` to enable.** When disabled, `/docs`, `/redoc`, and `/openapi.json` are not mounted (the proxy is an internal, server-to-server service).

```text
http://localhost:8002/docs
```

---

## System Prompt

The system prompt — the persona — is owned by **careeragent-api**, not `careeragent-infra`. `careeragent-api` loads it once at startup and sends it as the first `system` message in the messages list on every `/chat` request.

`careeragent-infra` has no knowledge of what the system prompt contains. It receives the full messages list and forwards it to Bedrock unmodified; the per-request reasoning effort is applied as a Bedrock thinking-depth parameter, not as message text. The persona text passes through untouched. The `/embed` route carries no persona — it forwards raw input strings only.

**careeragent-api is responsible for:**
- Owning and managing the persona (`src/prompt/bio.txt`, baked into its image).
- Including it as `{"role": "system", "content": "..."}` as the first message on every `/chat` request.
- Setting `reasoning_effort` per request based on the use case (or omitting it to let `careeragent-infra` default).

---

## SSE Stream Specification

(Applies to `/chat` only. `/embed` returns a single JSON body, not a stream.)

### Event format

```text
data: <JSON ChatCompletion chunk>\n\n
```

Each event payload is a JSON-encoded OpenAI ChatCompletion chunk. The double newline `\n\n` terminates each event.

### Stream lifecycle

```text
[connection established]
     │
     ▼
data: {... "delta": {"reasoning": "..."} ...}\n\n   ← thinking chain begins
     │
     ▼
data: {... "delta": {"reasoning": "..."} ...}\n\n   ← thinking continues
     │
     ▼
data: {... "delta": {"content": "..."} ...}\n\n     ← final answer tokens begin
     │
     ▼
data: {... "delta": {}, "finish_reason": "stop" ...}\n\n   ← terminal chunk
     │
     ▼
data: [DONE]\n\n                                    ← stream complete
     │
     ▼
[connection closed]
```

### Handling the reasoning chain

Both the base model and the control-layer model are reasoning models. Every response from either model can include a thinking chain (in `delta.reasoning`) before the final answer (in `delta.content`). (The embedding model is not a reasoning model and is not reached via this stream.)

Three options for handling on the caller side:

**Option 1 — Hide entirely:** Filter `delta.reasoning` tokens before displaying. Show only `delta.content`.

**Option 2 — Show in a collapsible:** Display reasoning in a collapsed "Show thinking" section, answer in the main surface. (This is what `careeragent-frontend` does.)

**Option 3 — Show everything:** Stream all tokens directly to the UI as-is.

The choice is entirely a caller/frontend decision. `careeragent-infra` streams both channels and takes no position on display.

---

## Technical Specifications

### Models

`careeragent-infra` routes between three logical models, each an Amazon Bedrock model id reached via LiteLLM. The actual weights, context window, and serving are Bedrock's concern and are not pinned by `careeragent-infra` — only the model id is configured.

**base model (primary)**

| Property | Value |
|---|---|
| Selector | `model="base"` on `/chat` (default; also the value when `model` is omitted) |
| Config | `BASE_MODEL` — a LiteLLM Bedrock model id, e.g. `bedrock/us.anthropic.claude-opus-4-8` |
| Type | Reasoning model — can emit a thinking chain before the answer |
| Role | Default model — all everyday CareerAgent conversations |

**nervous-system model (control layer)**

| Property | Value |
|---|---|
| Selector | `model="nervous_system"` on `/chat` |
| Config | `NERVOUS_SYSTEM_MODEL` (optional — when unset, the route is "not configured"), e.g. `bedrock/us.anthropic.claude-haiku-4-5` |
| Type | Reasoning model — fast control model |
| Role | Routing, history filtering, agent decisions |

**embedding model**

| Property | Value |
|---|---|
| Selector | `POST /embed` (caller sends no `model` field — route selected by `EMBEDDING_MODEL` config) |
| Config | `EMBEDDING_MODEL` (optional — when unset, `/embed` returns "not configured"), e.g. `bedrock/amazon.titan-embed-text-v2:0` |
| Type | Embedding model — turns text into vectors. Not a reasoning model. |
| Role | Text → vector for retrieval (resume sections, conversation history) |

> **Model availability.** Configure only model ids you have enabled in your Bedrock account and region. Most current Claude models on Bedrock require a **cross-region inference profile** (the `us.` prefix, e.g. `us.anthropic.claude-opus-4-8`) rather than the bare on-demand id. Confirm availability in the AWS Bedrock console for your region first.

> **Embedding dimension.** Titan Text Embeddings v2 emits 1024-dim vectors; Cohere embed v3 also emits 1024-dim. Whatever you choose, the dimension must match the pgvector column in careeragent-memory.

### Generation parameters

| Parameter | Value | Notes |
|---|---|---|
| `reasoning_effort` | `low` / `medium` / `high` | First-class `/chat` field — mapped to Claude's extended-thinking depth on the Bedrock call. Applies to the two chat models only. |
| `MAX_TOKENS` | int (default 8192) | Max output tokens per generation. Bedrock's Anthropic models require a bound; must exceed the thinking budget at high effort. |
| Batching | Bedrock-managed | For `/embed`, pass a list to batch multiple inputs in one call. |

### Infrastructure

| Property | Value |
|---|---|
| Inference provider | Amazon Bedrock (via LiteLLM) |
| Base model | `BASE_MODEL` (a LiteLLM Bedrock model id) |
| Control-layer model | `NERVOUS_SYSTEM_MODEL` (optional) |
| Embedding model | `EMBEDDING_MODEL` (optional) |
| Auth to Bedrock | AWS credential chain (env keys / profile / IAM role) + `AWS_REGION_NAME` |
| Scaling | Managed by AWS — Bedrock is always-on; no scale-to-zero, no cold start |

`careeragent-infra` itself runs no model and holds no weights — it is a thin async proxy. All compute lives in Bedrock.

### Container

| Property | Value |
|---|---|
| Base image | `python:3.12-slim` |
| WORKDIR | `/app` |
| Proxy port | `8002` (public) |
| Env file | `.env` at project root |
| GPU required | No — inference runs on Bedrock |
| Volume required | No — no weights, no state |

### Startup timing

| Phase | Approximate duration |
|---|---|
| Proxy startup | < 10 seconds (no model to load) |
| First Bedrock call | No cold start — Bedrock is always-warm; latency is generation time only |

Unlike a serverless GPU backend, Bedrock does not scale to zero, so there is no first-request cold start to absorb. The first `/chat` or `/embed` after an idle period responds at normal generation latency.

### Generation timing

| Scenario | Reasoning | Approximate duration |
|---|---|---|
| Simple greeting | low | seconds |
| Short factual question | medium | seconds to tens of seconds |
| Complex reasoning task | high | tens of seconds to minutes |
| Embedding | n/a | milliseconds to a couple of seconds |

Actual numbers depend on the model and load. Treat these as order-of-magnitude expectations, not guarantees.

---

## Integration Notes for Callers

`careeragent-api` is the primary caller of `careeragent-infra` today; other server-side services may call it as the system grows. The integration points:

### Readiness check pattern

Poll `/health` until `status` is `"ok"` to confirm the base route is configured and the AWS credentials resolve. `status` is `degraded` when the base model id or AWS credentials/region are missing.

```text
GET /health  →  {"status": "degraded", ...}   # base model id or AWS creds/region missing
GET /health  →  {"status": "ok", ...}          # base route configured and credentialed
```

Because `/health` does not call Bedrock, it confirms configuration, not live model access — a model you lack IAM access to still shows `ok` here and fails at call time.

### Selecting the model per request (`/chat`)

Pass `model` in the request body to route to a specific chat model. If omitted, every request routes to the base model — the pipeline is unbroken for callers that never set it. `careeragent-api` omits the field on every call today; the `nervous_system` route is available for callers that need a fast control model.

```text
{ "messages": [...], "reasoning_effort": "medium", "model": "base" }            # default
{ "messages": [...], "reasoning_effort": "low",    "model": "nervous_system" }  # control layer
```

### Embedding requests (`/embed`)

POST a string or a list of strings to `/embed` with the same `X-API-Key`. The response is OpenAI-compatible embeddings JSON; read `data[i].embedding` for each input. Batch by passing a list. `careeragent-infra` only turns text into vectors — storing and searching those vectors is the caller's concern. If `EMBEDDING_MODEL` is unset, `/embed` returns `503` and the `/chat` path is entirely unaffected.

### Setting reasoning effort per request (`/chat`)

Pass `reasoning_effort` in the request body. If omitted, the server default (`medium`, from the `REASONING_EFFORT` env var) applies. Applies to both chat models; the embedding model does not reason.

### Token handling (`/chat`)

Each SSE event is a JSON ChatCompletion chunk. The caller's decoder must `json.loads()` each `data:` payload and route `choices[0].delta.reasoning` and `choices[0].delta.content` to the appropriate surfaces. The stream terminates with an empty-delta `finish_reason: "stop"` chunk followed by `data: [DONE]`. Watch the stream for a `data: [ERROR] ...` payload too — that is how Bedrock-side failures surface on `/chat`.

### API key handling

`API_KEY` is the caller↔infra secret (the same value `careeragent-api` holds as `INFRA_API_KEY`). The AWS credentials never leave `careeragent-infra` — callers do not need them and never see them. Multiple server-side callers share `API_KEY` today; per-caller keys are a later evolution.

### Long generation times

A high-effort reasoning response can take tens of seconds to minutes. The caller should not set a short read timeout — `careeragent-api` uses a generous read timeout on this boundary for exactly this reason. The proxy bounds each Bedrock call at `REQUEST_TIMEOUT` (default 600s).

### CORS

`careeragent-infra` does not configure CORS headers. It is called server-side, never directly from a browser. This keeps the AWS credentials server-side and out of any client.

### The `[DONE]` sentinel (`/chat`)

Always handle `[DONE]` explicitly — it signals generation is complete and the connection can be closed.

### 401 handling

A `401` indicates a key configuration error between the caller and `careeragent-infra` (`INFRA_API_KEY` on the api side does not match `API_KEY` on the infra side). Log it for the operator — it is not an end-user-facing error.

---

## Environment Variables Reference

| Variable | Type | Default | Description |
|---|---|---|---|
| `API_KEY` | string | — | Secret validated against the `X-API-Key` header on `/chat` and `/embed`. Must match `careeragent-api`'s `INFRA_API_KEY` byte-for-byte. Required. |
| `AWS_REGION_NAME` | string | — | AWS region for Bedrock (e.g. `us-east-1`). Falls back to `AWS_REGION`. Required. |
| `AWS_ACCESS_KEY_ID` | string | — | AWS access key. Read by LiteLLM/boto3. Optional if an IAM role / shared profile provides credentials. Never logged. |
| `AWS_SECRET_ACCESS_KEY` | string | — | AWS secret key. Same handling as above. |
| `AWS_SESSION_TOKEN` | string | — | Only for temporary (STS) credentials. |
| `AWS_BEARER_TOKEN_BEDROCK` | string | — | Alternative auth: a Bedrock long-term API key (single bearer token). If set, leave the access-key/secret pair blank. `AWS_REGION_NAME` is still required. |
| `BASE_MODEL` | string | — | LiteLLM Bedrock model id for the base (coach) model. Default route for all `/chat` requests. Required. e.g. `bedrock/us.anthropic.claude-opus-4-8`. |
| `NERVOUS_SYSTEM_MODEL` | string | — | LiteLLM Bedrock model id for the control-layer model. Used when `model="nervous_system"`. Optional — when unset, that route reports "not configured". |
| `EMBEDDING_MODEL` | string | — | LiteLLM Bedrock model id for the embedding model. Used by `POST /embed`. Optional — when unset, `/embed` returns "not configured" and `/chat` is unaffected. e.g. `bedrock/amazon.titan-embed-text-v2:0`. |
| `REASONING_EFFORT` | string | `medium` | Server default reasoning level for the chat models. Mapped to Claude's thinking depth. Overridable per `/chat` request. |
| `MAX_TOKENS` | int | `8192` | Max output tokens per `/chat` generation. Must exceed the thinking budget at high effort. |
| `REQUEST_TIMEOUT` | float | `600` | Per-request timeout (seconds) for Bedrock calls. |
| `MODEL_RETRIES` | int | `5` | Retries for transient Bedrock errors (503 burst / 429 throttle), with exponential backoff. `/chat` retries only before the first streamed chunk; `/embed` retries fully. `0` disables. |
| `BACKOFF_CAP` | float | `20` | Max seconds for one retry's exponential backoff (jitter added on top). |
| `INFRA_ENABLE_DOCS` | string | `""` | When `"true"`, exposes `/docs`, `/redoc`, `/openapi.json`. Disabled by default. |

---

## Known Behaviors

| Behavior | Cause | Caller handling |
|---|---|---|
| Reasoning tokens before the final answer (`/chat`) | Both chat models are reasoning models — they can think before answering | Filter or display per UX choice (reasoning is in `delta.reasoning`, answer in `delta.content`) |
| `low` effort is faster but shallower | Less thinking | Use for lightweight calls only |
| `high` effort significantly slower | Extended thinking | Show a loading indicator; no short read timeout |
| `degraded` on `/health` | Base `BASE_MODEL` unset, or AWS credentials / `AWS_REGION_NAME` not resolvable | Set the model id and provide AWS credentials + region |
| `/health` shows `ok` but a call still fails | `/health` checks config + credential presence, not live Bedrock access (no IAM access to the model, throttling) | Treat the first real `/chat` / `/embed` as the true access check |
| Transient Bedrock 503/429 retried automatically | Bedrock throws 503 bursts / throttles on large requests; the proxy retries with backoff (`MODEL_RETRIES`) before surfacing an error | None — retries are transparent. Raise `MODEL_RETRIES` if bursts are large. `/chat` retries only before the first chunk |
| `[ERROR]` event inside a `/chat` 200 stream | Bedrock returned an error (throttle, access-denied, unavailable model) that outlasted the retries, or failed mid-stream after bytes were sent | Watch the SSE stream for `data: [ERROR] ...`, not just the HTTP status |
| `401` on a valid-looking request | `API_KEY` (infra) and `INFRA_API_KEY` (api) mismatch | Configuration error — align the two values |
| `422` on a malformed request | Pydantic validation failed | `/chat`: send a `messages` array with valid `role`/`content`. `/embed`: send `input` as a string or list of strings |
| `400` missing user message (`/chat`) | No `user`-role message in `messages` | Always include at least one user message |
| `400` empty input (`/embed`) | `input` is an empty string or empty list | Send non-empty text |
| `503` on `/embed` — "not configured" | `EMBEDDING_MODEL` not set in `.env` | Set the embedding model id |
| `502` on `/embed` | Bedrock returned an error embedding the input (e.g. access denied, throttle), or a proxy-level error | Inspect the operator logs for the underlying Bedrock error |
| `503` on `/chat` — "not configured" | The selected route's model id (`BASE_MODEL` / `NERVOUS_SYSTEM_MODEL`) is unset | Set the model id for that route |

---

## Design Decisions

### Why keep a FastAPI proxy instead of calling Bedrock directly from each caller?

Putting caller authentication in one place keeps the AWS credentials out of the gateway and frontend layers — only this service holds them. It also gives a stable, **OpenAI-shaped** contract regardless of what sits behind it: the backend moved from an OpenAI-compatible HTTP endpoint to Amazon Bedrock without any caller change, precisely because they only ever talk to this proxy's OpenAI-shaped edges. It is the single chokepoint every model path runs through — chat and embeddings alike.

### Why LiteLLM?

Bedrock does not speak the OpenAI wire format natively (different request/response shapes, a different streaming event format). LiteLLM normalizes Bedrock to the OpenAI shape on both ends, so the proxy can present the exact same `/chat` SSE and `/embed` JSON contract the rest of the stack already depends on, with far less translation code than calling `bedrock-runtime` directly. It is also the path openagent-code uses for its Bedrock support.

### Why a separate `/embed` endpoint instead of a `model` route on `/chat`?

`/chat` is welded to the messages-in / SSE-stream-out contract. Embeddings have a different request shape (raw input strings), a different response (a single JSON vector array, no streaming), and no reasoning. Forcing that through `/chat` would break the contract both ways, so embeddings get their own endpoint while reusing the same auth and the same Bedrock credentials.

### Why does `/embed` return real error codes when `/chat` cannot?

`/chat` commits an `HTTP 200` the moment the SSE stream begins, so a later Bedrock failure can only be reported as an in-stream `[ERROR]` event. `/embed` is a single, non-streaming response, so it can and does return real HTTP status codes (`400` / `502` / `503`).

### Why a caller key and AWS credentials (two boundaries)?

`API_KEY` authenticates the caller to `careeragent-infra`. The AWS credentials authenticate `careeragent-infra` to Bedrock. Separating them means the caller key can be rotated without touching AWS configuration, and the AWS credentials never leave this service. The caller-side validation is isolated in `verify_api_key` so it can later move from a single shared key to per-caller keys without changing the endpoint contract.

### Why reasoning effort as an API field?

Both chat models support configurable reasoning depth. Exposing it per request lets a frontend offer Quick / Standard / Deep modes and lets tooling set depth per use case. The proxy maps it to Claude's extended-thinking depth on Bedrock so callers never hand-write a thinking instruction.

### Why the OpenAI messages / embeddings format?

It makes `careeragent-infra` compatible with almost any frontend and model backend, keeps the serving layer stateless, and keeps the wire protocol standard and boring — which is what you want in an inference proxy. It is also what let the backend swap from an HTTP endpoint to Bedrock without touching a single caller.

### Why translate the stream instead of relaying bytes?

The previous OpenAI-compatible backend let the proxy relay the SSE byte-for-byte. Bedrock cannot — its streaming format is not OpenAI SSE — so the proxy must translate. It builds each OpenAI chunk by hand from LiteLLM's normalized output (mapping thinking to `delta.reasoning`, answer to `delta.content`) so the *output* contract is byte-compatible with what callers already parse, even though the relay path now does real work.

### Why port 8002?

Port convention: 8000 = careeragent-frontend, 8001 = careeragent-api, 8002 = careeragent-infra, 8003 = careeragent-logger. The numbering reflects the request flow.

---

*careeragent-infra — part of the CareerAgent system*
