# 0001 — careeragent-sessions

> The **conversation system-of-record** for CareerAgent. It owns conversation/session
> identity and the durable, ordered transcript, so conversations survive a page reload and
> can be listed and restored. It sits between `careeragent-frontend` and `careeragent-api`,
> relaying each turn unchanged. It is a new service — it changes **no existing service's code**.

---

## Goal

Today the frontend (Streamlit) holds the transcript only in `st.session_state`, so a page
reload wipes it; the gateway is stateless; and memory is scoped to a single static
`MEMORY_SESSION_ID=dev-session-001`, so every conversation pools into one bucket. There is no
way to give a conversation a stable identity, persist it as an ordered transcript, or list and
restore past conversations.

`careeragent-sessions` fixes exactly that gap, and nothing more: it **mints a per-conversation
id**, **persists the ordered transcript**, **exposes a history API**, and **transparently
relays** each `/chat` turn to `careeragent-api` (which keeps doing model + RAG + capture).

## Concepts

- **conversation** — a sequence of messages under a stable `conversation_id` (UUID), owned by
  this service. Carries a `title`, `created_at`, `updated_at`.
- **message** — `{role, content, idx, created_at}` within a conversation; `idx` gives a total
  order.
- **relay** — `sessions` forwards `POST /chat` to `careeragent-api`, streams the OpenAI-shaped
  SSE back to the caller byte-compatibly, and captures the assistant turn as it streams.
- **system of record vs. derived projections** — `sessions` holds the **canonical** transcript.
  `logger` (audit/training) and `memory` (RAG vectors) hold their own copies for *different*
  jobs and lifecycles. The three are **co-written** from the same turn, not synced — see
  Design Decisions.

## Where it sits

```
frontend (:8000) → careeragent-sessions (:8005) → careeragent-api (:8001) → infra / memory / logger
```

`careeragent-api` is unchanged: given a turn it does the model response, memory RAG, and logger
capture exactly as it does today. The frontend repoints `CAREERAGENT_API_URL` at `:8005` (a
`.env` change, no code) and keeps speaking the same `/chat` contract.

## Contract (HTTP)

### `POST /chat` — `X-API-Key` required
Request body (the same shape the frontend already sends, plus an optional id):
```json
{ "messages": [{"role":"user","content":"..."}], "reasoning_effort": "medium", "conversation_id": "<uuid?>" }
```
- `conversation_id` **optional**. Absent → **mint** a new one. Present → **upsert** (create if
  new, else append) — lets a caller own id generation.
- Behavior: persist the latest user turn under the conversation; relay to `careeragent-api`
  `/chat`; stream the SSE back unchanged; on **clean** completion (saw `data: [DONE]`, no
  `[ERROR]`) persist the assistant turn. An incomplete/errored stream does **not** persist a
  complete assistant turn.
- Response: `text/event-stream` — identical OpenAI ChatCompletion chunks + `data: [DONE]` — plus
  a response header **`X-Conversation-Id: <id>`**.
- Errors: `400` empty messages / no user message · `401` bad/missing key · upstream failure
  surfaces as an in-stream `data: [ERROR] …` + `[DONE]` (mirrors the existing infra→api pattern;
  the 200 stream is already committed).

### `GET /conversations` — `X-API-Key`
List conversations, newest first, paginated: `[{conversation_id, title, created_at, updated_at,
message_count}]`.

### `GET /conversations/{id}` — `X-API-Key`
Full ordered transcript: `{conversation_id, title, messages:[{role, content, idx, created_at}],
created_at, updated_at}`. `404` if unknown.

### `POST /conversations` — `X-API-Key`  *(optional convenience)*
Mint an empty conversation; returns `{conversation_id}`.

### `DELETE /conversations/{id}` — `X-API-Key`  *(optional)*
Remove a conversation and its messages.

### `GET /health` — no auth
House shape: `{status: "ok"|"degraded", sessions: "ok", database: "ok"|"unreachable",
upstream_api: "ok"|"unreachable"|"unknown"}`.

## Store (own Postgres, schema `careeragent_sessions`)

```
conversations(
  id           uuid primary key,
  title        text,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  metadata     jsonb not null default '{}'
)
messages(
  id              uuid primary key,
  conversation_id uuid not null references conversations(id) on delete cascade,
  idx             integer not null,            -- total order within the conversation
  role            text not null,               -- system | user | assistant
  content         text not null,
  created_at      timestamptz not null default now(),
  unique(conversation_id, idx)
)
```
**Every object is created in the `careeragent_sessions` schema** (schema-qualified, never bare
`public`) so the service can later share one Postgres instance with no code change — see
Design Decisions.

## Auth (compartmentalized, house pattern)

- **Inbound** `X-API-Key` = `SESSIONS_API_KEY` (frontend↔sessions). Constant-time compare;
  `/health` is unauthenticated.
- **Outbound** to `careeragent-api`: `X-API-Key` = the api's `CAREERAGENT_API_KEY`. A separate
  boundary key — never reuse the inbound one.

## Precedence / behavior rules

1. `conversation_id` absent → mint (server-authoritative UUID). Present → upsert; a malformed
   id is `400`.
2. `title` is derived from the first user message (first ~60 chars) when unset.
3. The assistant turn is persisted **only** on a clean stream completion. On `[ERROR]` or a
   dropped stream, the user turn is kept and the assistant turn is not recorded as complete.
4. Message `idx` is assigned server-side, monotonic per conversation; the caller cannot set it.
5. The relay never inspects or rewrites message content beyond persisting it — the model
   contract (persona injection, RAG, reasoning) all stay with `careeragent-api`.

## Acceptance

- [ ] `POST /chat` with no `conversation_id` mints one, returns `X-Conversation-Id`, persists
      the user + assistant turns in order, and streams byte-compatible SSE.
- [ ] `POST /chat` with an existing `conversation_id` appends the new turns in order.
- [ ] `GET /conversations` lists newest-first with correct `message_count`.
- [ ] `GET /conversations/{id}` returns the ordered transcript; unknown id → `404`.
- [ ] Missing/invalid `X-API-Key` → `401`; `GET /health` (no auth) → `200` with the documented shape.
- [ ] An upstream `careeragent-api` failure surfaces as in-stream `[ERROR]` + `[DONE]`, and no
      complete assistant turn is persisted for that turn.
- [ ] `init.sql` creates everything in schema `careeragent_sessions`; repointing
      `SESSIONS_DB_HOST` / `SESSIONS_DB_NAME` at a shared instance requires **no code change**.
- [ ] The existing services (infra, api, frontend, memory, logger) are unchanged.

## Non-goals (this spec)

- **Per-conversation memory (RAG) scoping** — making `memory` key off the real `conversation_id`
  needs a small future change in `careeragent-api` (accept a session id per request). Deferred;
  transcript persistence/restore works without it.
- **Visual restore in the Streamlit UI** — needs a small frontend "load-history-on-start" hook.
  Deferred. This service only *provides* the history API.
- **Multi-user / per-user ownership** — a single shared `SESSIONS_API_KEY` for now, matching the
  rest of the stack. Per-user identity is a later evolution.
- **Shared-instance DB as default** — supported by config; the default is a separate Postgres.

## Design Decisions

- **Why a third transcript copy?** `sessions` is the *system of record* for conversations;
  `logger` is an append-only audit/training sink (retention-bound — it *purges* old conversations,
  so it can't back the UI) and `memory` is RAG vectors (deduped, top-k, not a chronological
  transcript). Three projections of the same turn, each shaped for its job; the "duplicate" is a
  few KB of text. CQRS / polyglot-persistence, on purpose.
- **Why in front of `api`, not beside it?** One single point of contact for the frontend, while
  `api` stays the per-turn orchestrator, untouched. No two services fighting over "I coordinate."
- **Why own DB now, shared later?** Separate is simpler and isolated for dev. Schema-qualifying
  everything to `careeragent_sessions` + an env-driven connection keeps "share one Postgres
  instance" a config-only switch (point `SESSIONS_DB_*` at the shared instance; pgvector is
  needed only if `memory` joins that instance).
- **Why mint-and-return `conversation_id`?** Server-authoritative ids that the frontend adopts in
  Phase 2 (when it sends the id back and loads history) — without forcing a frontend change now.

---

*careeragent-sessions — part of the CareerAgent system. Port 8005.*
