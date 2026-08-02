# careeragent-sessions — Datasheet

> Reference document for building on top of careeragent-sessions.
> Audience: **careeragent-frontend** (the caller) and any operator wiring the stack.

---

## Quick Reference

| Item | Value |
|---|---|
| Role | Conversation system-of-record for CareerAgent |
| Base URL | `http://localhost:8005` |
| Position | `frontend → careeragent-sessions → careeragent-api` |
| Auth in (caller) | `X-API-Key` (required on all but `/health`) |
| Auth out (api) | `X-API-Key` = `CAREERAGENT_API_KEY` |
| Streaming | SSE on `/chat` (OpenAI ChatCompletion chunks, `[DONE]`) |
| Store | Own Postgres, schema `careeragent_sessions` (conversations + messages) |
| Port | `8005` |
| Version | 0.1.0 |

---

## Overview

`careeragent-sessions` owns conversation identity and the durable, ordered transcript. The frontend talks to it instead of `careeragent-api`; it persists each turn, relays `/chat` to `careeragent-api` unchanged (which does model + RAG + capture), streams the SSE back byte-for-byte, and exposes a history/restore API. It is the **canonical** conversation store; logger (audit/training) and memory (RAG) are derived projections.

`careeragent-api` is **not modified** by this service. The integration is: the frontend repoints `CAREERAGENT_API_URL` at `:8005` — a config change.

---

## Authentication

- **Inbound** `X-API-Key` = `SESSIONS_API_KEY` (frontend↔sessions). Constant-time compare; `/health` is unauthenticated.
- **Outbound** to `careeragent-api`: `X-API-Key` = `CAREERAGENT_API_KEY` (must match api's inbound key). A separate boundary — never reuse the inbound key.

---

## API Reference

### `POST /chat`  — `X-API-Key`
Request:
```json
{ "messages": [{"role":"user","content":"..."}], "reasoning_effort": "medium", "conversation_id": "<uuid?>" }
```
- `conversation_id` optional. Absent → mint. Present (valid UUID) → upsert/continue. Malformed → `400`.
- Persists the new user turn, relays to `careeragent-api /chat`, streams the SSE back.
- On clean completion (saw `data: [DONE]`, no `[ERROR]`) the assistant turn is persisted; an errored/dropped stream is not recorded as complete.

Response: `text/event-stream` (identical OpenAI ChatCompletion chunks ending `data: [DONE]`), header `X-Conversation-Id: <id>`.

Errors: `400` empty messages / no user message / bad UUID · `401` bad key · upstream failure surfaces as in-stream `data: [ERROR] …` + `[DONE]`.

### `GET /conversations`  — `X-API-Key`
Query: `limit` (1–200, default 50), `offset`. Returns newest-first:
```json
[{"conversation_id":"...","title":"...","created_at":"...","updated_at":"...","message_count":4}]
```

### `GET /conversations/{id}`  — `X-API-Key`
```json
{"conversation_id":"...","title":"...","created_at":"...","updated_at":"...",
 "messages":[{"role":"user","content":"...","idx":0,"created_at":"..."}]}
```
`404` if unknown.

### `POST /conversations`  — `X-API-Key`
Body `{ "title"?: "..." }` → `{ "conversation_id": "<uuid>" }`.

### `DELETE /conversations/{id}`  — `X-API-Key`
`{ "deleted": "<id>" }`, or `404`.

### `GET /health`  — no auth
```json
{"status":"ok|degraded","sessions":"ok","database":"ok|unreachable","upstream_api":"ok|unreachable"}
```
`status` is `ok` when the database is reachable.

---

## Store

Schema `careeragent_sessions` (all objects schema-qualified — keeps the shared-instance switch config-only):

```
conversations(id uuid pk, title text, created_at, updated_at, metadata jsonb)
messages(id uuid pk, conversation_id uuid fk→conversations on delete cascade,
         idx int, role text, content text, created_at, unique(conversation_id, idx))
```
`idx` is server-assigned, monotonic per conversation. `init.sql` creates these on first DB boot.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SESSIONS_API_KEY` | — | Inbound X-API-Key. Required. |
| `CAREERAGENT_API_URL` | `http://careeragent-api:8001` | Relay target. |
| `CAREERAGENT_API_KEY` | — | Outbound key (matches api's inbound). Required. |
| `SESSIONS_DB_USER` | `careeragent_sessions` | DB role. |
| `SESSIONS_DB_PASSWORD` | — | DB password. Required. |
| `SESSIONS_DB_HOST` | `sessions-db` | DB host (compose sets this). |
| `SESSIONS_DB_PORT` | `5432` | DB port. |
| `SESSIONS_DB_NAME` | `careeragent_sessions` | DB name. |
| `SESSIONS_DB_SCHEMA` | `careeragent_sessions` | Schema for all objects. |
| `SESSIONS_DATABASE_URL` | — | Optional full URL (overrides parts). |
| `SESSIONS_PORT` | `8005` | Listen port. |
| `SESSIONS_ENABLE_DOCS` | `false` | Expose `/docs` when `true`. |

---

## Known Behaviors

| Behavior | Note |
|---|---|
| Same `/chat` body as the frontend already sends | + optional `conversation_id`; no frontend code change to relay |
| Assistant turn persisted only on clean stream | `[ERROR]`/dropped stream keeps the user turn, not a half-answer |
| `message_count` includes both user and assistant turns | |
| Concurrent turns on one conversation | `idx` is sequential; not hardened for simultaneous writes to the same conversation (single-user tool) |

---

## Design Decisions

- **Third transcript copy, on purpose** — system-of-record vs. audit (logger) and RAG (memory) projections; different shapes and lifecycles (logger retention would purge old chats).
- **In front of `api`, not beside** — single frontend contact; `api` stays the untouched per-turn orchestrator.
- **Own DB now, shared later** — schema-qualified + env-driven → config-only switch.
- **Server-authoritative `conversation_id`** — minted and returned; frontend adopts it in Phase 2.

## Non-goals (v0.1.0)

- Per-conversation **memory (RAG)** scoping (needs a small careeragent-api change — deferred).
- **Visual restore** in the Streamlit UI (needs a small frontend hook — deferred; this service provides the history API).
- Multi-user / per-user ownership (single shared key for now).

---

*careeragent-sessions — part of the CareerAgent system. Port 8005.*
