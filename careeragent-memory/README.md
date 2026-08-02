# careeragent-memory

> **The session-scoped retrieval layer for CareerAgent** — the FastAPI service that
> stores conversation turns as vectors (embedded through `careeragent-infra`'s `/embed`
> route) and returns the **top-k most relevant earlier turns** to `careeragent-api`.
> It ranks; it does not generate, and it does not build the prompt. `careeragent-api`
> builds the final query from what memory hands back.

| | |
|---|---|
| **Version** | 0.1.0 |
| **Port** | 8004 |
| **Base image** | python:3.11-slim |
| **Database** | PostgreSQL 16 + pgvector (memory-owned, **not** the shared instance) |
| **Status** | working — pre-production |

---

## Overview

`careeragent-memory` is the retrieval half of CareerAgent's RAG loop. The embedding half already exists: `careeragent-infra` ships a `/embed` route whose stated purpose is "embedding conversation turns before storing them in a vector database, and embedding a query at retrieval time." `careeragent-infra`'s datasheet then deliberately punts on the rest — "storing and searching those vectors (the vector database and retrieval logic) is the caller's concern." `careeragent-memory` is that caller.

It owns exactly one concern: **store a session's turns as vectors, and return the K earlier turns most relevant to the current one.** It does not own the persona, the prompt, the model, the conversation state, or the capture pipeline — those live in the other services, untouched.

The scope is deliberately narrow: **current session only.** Memory searches within one conversation, not across a user's history. The use case is long single conversations that would otherwise pressure the model's context window — instead of forwarding every turn, `careeragent-api` forwards the recent turns plus the handful of older turns memory flagged as relevant.

Two endpoints carry the whole job:

- **`POST /ingest`** — `careeragent-api` pushes a turn (the user's input, then the agent's output) into the store. Memory embeds it and writes the vector.
- **`POST /retrieve`** — `careeragent-api` sends the current query; memory embeds it, searches that session's stored vectors, and returns the top-k earlier turns with scores.

---

## The division of labor

This is the line that matters most:

```text
careeragent-memory  →  RANKS.  Stores turns, returns top-k relevant ones + scores.
careeragent-api     →  BUILDS.  Assembles the final messages list and calls the model.
```

`careeragent-memory` returns *candidates with relevance scores* and nothing more. It does not decide how many recent turns to keep verbatim, it does not splice anything into a prompt, and it does not talk to `/chat`. `careeragent-api` takes memory's output and constructs the final query:

```text
final messages = [ system: bio.txt ]
               + [ retrieved older turns from memory ]
               + [ the most recent N turns, verbatim ]
               + [ the current user turn ]
```

Keeping assembly in `careeragent-api` is intentional: prompt construction is identity/product-layer logic, and `careeragent-api` already owns the persona and is the only thing that talks to `/chat`. Memory stays a pure ranking function — easy to reason about, easy to fail open.

---

## Where this fits

```text
User → careeragent-frontend (:8000) → careeragent-api (:8001) ──/chat──▶ careeragent-infra (:8002) → provider
                                          │
                                          ├──/retrieve──▶ careeragent-memory (:8004) ──/embed──▶ careeragent-infra (:8002)
                                          ├──/ingest  ──▶ careeragent-memory (:8004) ──/embed──▶ careeragent-infra (:8002)
                                          │
                                          └──fire-and-forget──▶ careeragent-logger (:8003)
                                                                       │
                                          careeragent-memory ──▶ memory-db (own Postgres + pgvector)
```

Port 8004 is the next in sequence (8000 frontend, 8001 api, 8002 infra, 8003 logger, 8004 memory). Only `careeragent-api` calls `careeragent-memory`; `careeragent-memory` calls only `careeragent-infra` (`/embed`) and its own database. It is never reached from a browser.

---

## Per-turn flow

```text
user message arrives  (this turn's query)
  careeragent-api → memory  POST /retrieve {session_id, query, top_k}   ← searches PRIOR stored turns
  careeragent-api builds the final query, → infra /chat, streams the answer to the user
  careeragent-api → memory  POST /ingest {session_id, role:"user",      content:<input_text>}
  careeragent-api → memory  POST /ingest {session_id, role:"assistant", content:<output_text>}
```

`careeragent-api` already computes `input_text` and the assembled `output_text` for the logger's `conversation_capture`, so ingest reuses exactly that data. The assistant-response ingest happens after the answer has streamed, so it is off the user's critical path.

---

## API

### `POST /ingest`

Embed one turn and store its vector. Authenticated (`X-API-Key: MEMORY_API_KEY`) **and** HMAC-signed (the body carries a signed envelope — see [Security model](#security-model-summary)).

```json
{
  "request_id": "0d4c…",
  "client_timestamp": "2026-06-14T18:24:01.342000+00:00",
  "source_service": "careeragent-api",
  "hmac_signature": "<64-char lowercase hex>",
  "session_id": "cf81...d",
  "role": "user",
  "content": "How does the SSE relay work?"
}
```

Returns `201`:

```json
{ "session_id": "cf81...d", "role": "user", "stored": true, "duplicate": false, "id": "…uuid…" }
```

Re-ingesting identical content within a session is a no-op (`stored:false, duplicate:true`). **Ingest does not fail open silently** — a dropped ingest would invisibly drop a turn from all future retrieval. If embedding is unavailable it returns `503` so `careeragent-api` can log/retry off the user's path.

### `POST /retrieve`

Rank a session's stored turns against the current query. Authenticated and HMAC-signed (same envelope as `/ingest`). Always `200`.

```json
{
  "request_id": "9af1…",
  "client_timestamp": "2026-06-14T18:24:02.108000+00:00",
  "source_service": "careeragent-api",
  "hmac_signature": "<64-char lowercase hex>",
  "session_id": "cf81...d",
  "query": "and how does that interact with the logger?",
  "top_k": 5
}
```

```json
{
  "session_id": "cf81...d",
  "retrieved": [
    { "id": "…", "role": "assistant", "content": "careeragent-api pumps bytes…", "score": 0.83, "created_at": "…" }
  ],
  "degraded": false
}
```

`degraded: true` means retrieval failed open somewhere on the hot path — the embedding call (cold/unreachable/unconfigured) **or** the store search (DB error, embedding-dimension mismatch); the list is empty and `careeragent-api` proceeds with recent turns only.

### `GET /health`

Authenticated. Reports memory's own readiness; embedding reachability is informational and does **not** flip the service unhealthy (memory fails open).

```json
{
  "status": "ok",
  "careeragent_memory": { "version": "0.1.0", "store": "connected" },
  "careeragent_infra_embed": { "url": "http://careeragent-infra:8002/health", "status": "ok" }
}
```

### `GET /` and `GET /docs`

`GET /` is unauthenticated service identification (for platform probes that can't send the key). `GET /docs` is the Swagger UI.

---

## Security model (summary)

Two independent inbound secrets, one reused outbound secret — the same compartmentalized pattern as the rest of the system:

```text
careeragent-api ──MEMORY_API_KEY + MEMORY_HMAC_SECRET──▶ careeragent-memory ──INFRA_API_KEY──▶ careeragent-infra (/embed)
```

- `MEMORY_API_KEY` — inbound transport. Validated constant-time on every `/retrieve`, `/ingest`, `/health`.
- `MEMORY_HMAC_SECRET` — inbound integrity. A **second, independent** secret. Every `/retrieve` and `/ingest` body carries a signed envelope (`request_id`, `client_timestamp`, `source_service`, `session_id`, `hmac_signature`); memory verifies the HMAC-SHA256 signature constant-time and checks `client_timestamp` against a replay/freshness window (`MEMORY_REPLAY_WINDOW_SECONDS`, default 300s). An invalid signature or stale timestamp returns `401` (generic `Invalid or missing signature`; the specific reason is logged server-side only). Memory **refuses to boot without both** secrets.
- `INFRA_API_KEY` — outbound. The **same value** `careeragent-infra` accepts as its `API_KEY` (it shares one caller key across server-side callers today, a path its datasheet anticipates for "a retrieval layer embedding queries via `/embed`").

The signature is HMAC-SHA256 over a six-field canonical string — `{request_id}|{client_timestamp}|{operation}|{source_service}|{session_id}|{payload_hash}` — where `operation` is `ingest` or `retrieve` and `payload_hash` covers `{role, content}` (ingest) or `{query}` plus `{top_k}` when present (retrieve). It matches `careeragent-api/src/client/memory.py` byte-for-byte. `/health` and `/` have no body to sign and stay transport-key only.

This **reverses the earlier transport-key-only stance**: the api → memory boundary now uses the same signed-boundary discipline as api → logger. A leaked `MEMORY_API_KEY` alone can no longer forge or tamper an ingest/retrieve request — an attacker would also need `MEMORY_HMAC_SECRET`. A memory compromise exposes `MEMORY_API_KEY`, `MEMORY_HMAC_SECRET`, and `INFRA_API_KEY` — it cannot reach the frontend boundary, the logger, or the provider.

---

## Storage — memory owns its own database

`careeragent-memory` runs **its own** Postgres (`memory-db`, the `pgvector/pgvector:pg16` image), separate from the logger's shared instance. This keeps memory genuinely deployable on its own and keeps conversation vectors out of the logger's blast radius.

- One table, `careeragent_memory.turns`: `id, session_id, role, content, content_hash, embedding (unsized vector), created_at`.
- Search is **exact** cosine over a bounded per-session set — no ANN index, so `embedding` is an unsized `vector` and the dimensionality follows whatever the BYOC embedder emits.
- DDL lives in `database/init.sql`, applied once at the DB's first boot; the service never issues DDL at runtime.

---

## Setup

```bash
git clone https://github.com/william-mckeon/careeragent-memory.git
cd careeragent-memory
cp .env.example .env        # fill MEMORY_API_KEY, MEMORY_HMAC_SECRET, INFRA_API_KEY, CAREERAGENT_INFRA_URL, MEMORY_DB_PASSWORD
docker compose up -d --build
```

`docker compose` brings up both the service and its `memory-db`. Both attach to the external `careeragent-network` (created by `careeragent-logger` per the `careeragent-os` runbook), which is how memory reaches `careeragent-infra` by name.

Verify:

```bash
curl -H "X-API-Key: <MEMORY_API_KEY>" http://localhost:8004/health
```

Generate each inbound secret with `python -c "import secrets; print(secrets.token_hex(32))"` — `MEMORY_API_KEY` and `MEMORY_HMAC_SECRET` are independent values, and both must match what `careeragent-api` is configured to send. `INFRA_API_KEY` is copied from `careeragent-infra`'s `.env` (its `API_KEY`). Retrieval only returns results when `careeragent-infra`'s `EMBEDDING_MODEL_URL` is configured; without it, `/retrieve` still answers `200` but always `degraded: true`.

---

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `MEMORY_API_KEY` | Yes | — | Inbound transport secret (`X-API-Key`). Refuses to start if unset. |
| `MEMORY_HMAC_SECRET` | Yes | — | Inbound HMAC secret for the signed envelope on `/retrieve` and `/ingest`. Independent of `MEMORY_API_KEY`. Refuses to start if unset. |
| `MEMORY_REPLAY_WINDOW_SECONDS` | No | `300` | Max allowed skew (s) between `client_timestamp` and server time before a signed request is rejected `401`. |
| `INFRA_API_KEY` | Yes | — | Outbound secret to `careeragent-infra`. Must match its `API_KEY`. |
| `CAREERAGENT_INFRA_URL` | Yes | — | Base URL of `careeragent-infra`. No trailing slash. |
| `MEMORY_DATABASE_URL` | No | — | Full async URL; if set, the components below are ignored. |
| `MEMORY_DB_USER` | No | `careeragent_memory` | DB role. |
| `MEMORY_DB_PASSWORD` | Yes* | — | DB password (*required if `MEMORY_DATABASE_URL` is unset). |
| `MEMORY_DB_HOST` | No | `memory-db` | DB host. |
| `MEMORY_DB_PORT` | No | `5432` | DB port. |
| `MEMORY_DB_NAME` | No | `careeragent_memory` | DB name. |
| `MEMORY_TOP_K_DEFAULT` | No | `5` | Default `top_k` when omitted. |
| `MEMORY_MIN_SCORE` | No | `0.0` | Cosine-similarity floor; applied in-database before the `top_k` limit so it never eats into the budget. |
| `MEMORY_EMBED_TIMEOUT` | No | `10.0` | Read timeout (s) on `/embed` before failing open. |
| `MEMORY_EMBED_CONNECT_TIMEOUT` | No | `5.0` | Connect timeout (s) to `careeragent-infra`. |
| `MEMORY_PORT` | No | `8004` | Listen port. |
| `MEMORY_LOG_LEVEL` | No | `INFO` | DEBUG \| INFO \| WARNING \| ERROR \| CRITICAL. |

---

## Design decisions

**Session-scoped, exact search, no ANN index.** A session is a bounded set; exact cosine over a per-session filter is fast and exact, and it lets the vector column stay unsized so any BYOC embedding dimensionality works.

**Memory owns its own database.** Co-tenanting the logger's shared Postgres would have made memory's storage reach into the logger's image and bring-up. A separate DB keeps memory deployable on its own and isolates blast radius — the compartmentalization philosophy applied to data, not just keys.

**Memory calls `/embed` directly.** Keeps `careeragent-api` thin and puts every retrieval concern in one place; `careeragent-infra`'s datasheet explicitly anticipates this caller.

**`careeragent-api` builds the final query, not memory.** Prompt assembly is product-layer logic owned by the gateway; memory stays a pure ranking function, which keeps the fail-open path trivial.

**Retrieve fails open; ingest does not.** Any failure on the retrieve hot path — the embedding call or the store search (DB error, dimension mismatch) — degrades answer quality but never blocks `/chat`. A failed ingest, if swallowed, would silently drop a turn from future retrieval — so ingest surfaces a `503` instead, letting the caller react off the user's path.

**HMAC integrity on the inbound boundary.** This reverses an earlier transport-key-only stance. A second, independent secret (`MEMORY_HMAC_SECRET`) signs an envelope over every `/retrieve` and `/ingest` body, with a replay/freshness window on `client_timestamp`. A leaked transport key alone can no longer forge or tamper a request, and the api → memory boundary now uses the same signed-boundary discipline as api → logger — one integrity model across the system rather than two.

---

## Known limitations

- **Session-scoped only.** No cross-session/long-term memory by design. Expanding scope means swapping the store behind `src/store.py`'s interface.
- **First-turn cold-start latency.** The first `/embed` after the embedder scales to zero waits for spin-up; `MEMORY_EMBED_TIMEOUT` bounds how long before retrieval fails open.
- **Requires the embedder configured.** If `careeragent-infra`'s `EMBEDDING_MODEL_URL` is unset, retrieval is permanently `degraded` and ingest returns `503`. `/chat` is unaffected.
- **Exact search won't scale to large corpora.** Right for session scope; not a substitute for an ANN index at cross-session scale.
- **Relevance is similarity-only.** v1 ranks purely by cosine; using the `nervous_system` model for a smarter keep/drop decision is the intended next step.

---

## License

Copyright © 2026 William McKeon. Licensed under the Apache License, Version 2.0. See `LICENSE`.

---

## Maintainer

**William McKeon** ([github.com/william-mckeon](https://github.com/william-mckeon))

---

*careeragent-memory — part of the CareerAgent system*
