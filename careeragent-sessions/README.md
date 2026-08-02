# careeragent-sessions

> **The conversation system-of-record for CareerAgent** — owns conversation/session identity and the durable, ordered transcript so chats survive a reload and can be listed and restored.

---

## Overview

`careeragent-sessions` is the conversation layer of the CareerAgent system. It sits between `careeragent-frontend` and `careeragent-api`: it mints a per-conversation id, persists the ordered transcript in its own Postgres, exposes a history/restore API, and **relays each `/chat` turn to `careeragent-api` unchanged** (which keeps doing the model response, memory RAG, and logger capture).

It exists because the frontend is stateless (a reload wipes the on-screen transcript) and memory was scoped to a single static session id. `sessions` fixes exactly that gap — and nothing more. It is a **new service that changes no existing service's code**; the frontend repoints at it via one `.env` value.

It is the **system of record** for conversations; `careeragent-logger` (audit/training) and `careeragent-memory` (RAG vectors) keep their own copies for different jobs and lifecycles — three projections of the same turn, co-written, each shaped for its purpose.

---

## Where This Fits

```text
frontend (:8000) → careeragent-sessions (:8005) → careeragent-api (:8001) → infra / memory / logger
```

`careeragent-api` is unchanged — given a turn it does model + RAG + capture exactly as before. `sessions` adds conversation identity, persistence, and history around it.

**Port convention:** 8000 frontend · 8001 api · 8002 infra · 8003 logger · 8004 memory · **8005 sessions**.

---

## Architecture

```text
┌───────────────────────────────────────────────┐
│  careeragent-sessions  (port 8005)              │
│  FastAPI — src/backend/api.py                  │
│                                                │
│  POST /chat        → persist user turn         │
│                    → relay to careeragent-api    │
│                    → stream SSE back (unchanged)│
│                    → capture+persist assistant  │
│  GET  /conversations         → list            │
│  GET  /conversations/{id}    → full transcript │
│  GET  /health      → db + upstream-api status  │
└───────┬───────────────────────────┬────────────┘
        │ relay /chat (httpx)        │ persist (asyncpg)
        ▼                            ▼
  careeragent-api (:8001)        careeragent-sessions-db (Postgres)
                                 schema: careeragent_sessions
```

### Request flow — `/chat`
1. Auth (`X-API-Key` = `SESSIONS_API_KEY`).
2. Resolve `conversation_id` — mint if absent, upsert if provided.
3. Persist the new user turn.
4. Relay to `careeragent-api /chat`; stream the OpenAI-shaped SSE back byte-for-byte (plus an `X-Conversation-Id` header).
5. Capture the assistant answer off the stream; on clean completion (`[DONE]`, no `[ERROR]`), persist it.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Base image | `python:3.12-slim` |
| API | FastAPI + uvicorn (async) |
| Persistence | PostgreSQL via SQLAlchemy (async) + asyncpg |
| Relay | httpx (streaming) |
| Auth | `X-API-Key` (inbound) + a separate outbound key to api |
| Containerization | Docker + Docker Compose |

---

## Prerequisites

- Docker Desktop.
- The external network: `docker network create careeragent-network`.
- `careeragent-api` reachable on the network (this service relays to it).
- A `.env` (from `.env.example`) with `SESSIONS_API_KEY`, the upstream `CAREERAGENT_API_KEY` (matching api's), and `SESSIONS_DB_PASSWORD`.

---

## Project Structure

```text
careeragent-sessions/
├── docker/sessions/Dockerfile
├── database/init.sql            # conversations + messages, schema careeragent_sessions
├── src/
│   ├── backend/api.py           # FastAPI app — endpoints + relay/capture
│   ├── client/api_client.py     # streaming client to careeragent-api
│   ├── store.py                 # conversation/message persistence
│   ├── schemas.py               # pydantic models
│   └── security.py              # X-API-Key auth
├── tests/                       # hermetic (auth, db-url, SSE scanner)
├── specs/0001-sessions.md       # the contract
├── docker-compose.yml           # sessions + its Postgres, on careeragent-network
├── requirements.txt / requirements-dev.txt
├── .env.example
└── docs/DATASHEET.md
```

---

## Setup

```bash
cp .env.example .env            # set SESSIONS_API_KEY, CAREERAGENT_API_KEY, SESSIONS_DB_PASSWORD
docker network create careeragent-network   # if not already created
docker compose up -d --build
curl http://localhost:8005/health
```

---

## API Reference (summary)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/chat` | X-API-Key | Relay a turn; persist transcript; returns SSE + `X-Conversation-Id` |
| `GET` | `/conversations` | X-API-Key | List conversations (newest first) |
| `GET` | `/conversations/{id}` | X-API-Key | Full ordered transcript (404 if unknown) |
| `POST` | `/conversations` | X-API-Key | Mint an empty conversation |
| `DELETE` | `/conversations/{id}` | X-API-Key | Remove a conversation |
| `GET` | `/health` | none | `{status, sessions, database, upstream_api}` |

`POST /chat` body: `{ "messages": [...], "reasoning_effort"?: "low|medium|high", "conversation_id"?: "<uuid>" }` — the same shape the frontend already sends, plus the optional id. See `docs/DATASHEET.md` for the full contract.

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `SESSIONS_API_KEY` | — | Inbound X-API-Key (frontend↔sessions). Required. |
| `CAREERAGENT_API_URL` | `http://careeragent-api:8001` | Where `/chat` is relayed. |
| `CAREERAGENT_API_KEY` | — | Outbound key; must match careeragent-api's `CAREERAGENT_API_KEY`. |
| `SESSIONS_DB_USER/PASSWORD/HOST/PORT/NAME` | see `.env.example` | DB connection parts. |
| `SESSIONS_DB_SCHEMA` | `careeragent_sessions` | Schema for all objects — keeps the shared-instance switch config-only. |
| `SESSIONS_DATABASE_URL` | — | Optional full URL, overrides the parts. |
| `SESSIONS_PORT` | `8005` | Listen port. |
| `SESSIONS_ENABLE_DOCS` | `false` | Expose `/docs` when `true`. |

---

## Design Decisions

- **A third transcript copy, on purpose.** `sessions` is the canonical conversation store; `logger` is an append-only audit/training sink (retention-bound — it *purges* old chats) and `memory` is RAG vectors (deduped, top-k, not a chronological transcript). Three projections of the same turn, each shaped for its job.
- **In front of `api`, not beside it.** One single point of contact for the frontend; `api` stays the per-turn orchestrator, untouched.
- **Own DB now, shared later — for free.** Everything is schema-qualified to `careeragent_sessions` and the connection is env-driven, so pointing at a shared Postgres instance is config-only (pgvector needed only if `memory` joins).
- **Mint-and-return `conversation_id`.** Server-authoritative ids the frontend adopts in Phase 2 (send the id back + load history), without forcing a frontend change now.

---

## License

Apache License 2.0 — see `LICENSE`.

## Maintainer

**William McKeon**
