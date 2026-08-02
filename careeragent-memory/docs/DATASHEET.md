# careeragent-memory — DATASHEET

> Cross-service contract reference. The README is for newcomers; this file is for
> engineers integrating with careeragent-memory (today: careeragent-api) or auditing
> what it owns.

| Field | Value |
|---|---|
| **Service name** | `careeragent-memory` |
| **Version** | 0.1.0 |
| **Role** | Session-scoped retrieval layer (RAG) for the CareerAgent system |
| **Port** | 8004 |
| **Base image** | `python:3.11-slim` |
| **Runtime user** | `careeragent` (uid 1000) |
| **Framework** | FastAPI + uvicorn |
| **Database** | PostgreSQL 16 + pgvector — **memory-owned**, not the shared instance |
| **Auth in** | `X-API-Key: MEMORY_API_KEY` (transport) **+** HMAC-SHA256 signed envelope (`MEMORY_HMAC_SECRET`) on `/retrieve`, `/ingest`; `X-API-Key` only on `/health` |
| **Auth out** | `X-API-Key: INFRA_API_KEY` (on `/embed` calls to careeragent-infra) |
| **Inbound endpoints** | `POST /retrieve`, `POST /ingest`, `GET /health`, `GET /`, `GET /docs` |
| **Outbound consumed** | `POST /embed`, `GET /health` (careeragent-infra) |
| **Session store** | Per-turn rows in `careeragent_memory.turns` |
| **Status** | working — pre-production |

---

## 1. Ownership boundaries

### What this service owns

| Domain | Concrete artifact |
|---|---|
| Retrieval endpoint | `POST /retrieve` |
| Ingestion endpoint | `POST /ingest` |
| Vector storage | `careeragent_memory.turns` in memory's own Postgres |
| The storage seam | `src/store.py` (swappable backing store) |
| Ranking / fail-open policy | `src/retrieval.py` |
| Inbound auth | `src/security.py` (transport `MEMORY_API_KEY` + HMAC `MEMORY_HMAC_SECRET`, both constant-time) |
| The outbound `/embed` boundary | `src/client/infra.py` (`INFRA_API_KEY`) |
| The schema definition | `database/init.sql` |

### What this service does NOT own

| Concern | Owner |
|---|---|
| Embedding / inference | careeragent-infra (`/embed`) → BYOC provider |
| Provider credential (`PROVIDER_API_KEY`) | careeragent-infra |
| Prompt assembly / the final query | careeragent-api |
| Persona / system prompt | careeragent-api |
| In-session conversation state | careeragent-frontend |
| Event capture & audit | careeragent-logger |
| Cross-session / long-term memory | Not implemented (session-scoped by design) |
| PII stripping | Not implemented (turns stored raw) |
| Rate limiting | Reverse proxy |

---

## 2. Inbound contracts

All bodies are `application/json`; all responses are `application/json`.
`/retrieve`, `/ingest`, and `/health` require `X-API-Key: <MEMORY_API_KEY>`.
`/retrieve` and `/ingest` additionally carry a signed envelope in the body (see
2.0); `/health` and `/` have no body to sign and remain transport-key only.

### 2.0 Signed envelope (HMAC integrity)

Two independent secrets gate the api → memory boundary, compartmentalized like
careeragent-logger: `MEMORY_API_KEY` (transport, `X-API-Key` header) and
`MEMORY_HMAC_SECRET` (payload integrity + replay protection). Memory refuses to
boot without **both**. A leaked `MEMORY_API_KEY` alone can no longer forge or
tamper an ingest/retrieve request — the attacker would also need
`MEMORY_HMAC_SECRET`.

Every `POST /ingest` and `POST /retrieve` body carries these envelope fields
alongside the operation payload:

| Field | Type | Notes |
|---|---|---|
| `request_id` | string 1..128 | Caller-minted id for this request. Signed. |
| `client_timestamp` | string (datetime) | Canonical UTC ISO-8601 with microseconds and `+00:00` offset (e.g. `2026-06-14T18:24:01.342000+00:00`). Signed; checked against the replay window. |
| `source_service` | string 1..64 | Calling service name (e.g. `careeragent-api`). Signed. |
| `session_id` | string 1..128 | Scopes the turn/search. Signed. |
| `hmac_signature` | string (64-char lowercase hex) | HMAC-SHA256 over the canonical string below. |

**Canonical string** — HMAC-SHA256 (key = `MEMORY_HMAC_SECRET`) over six
pipe-separated fields:

```text
{request_id}|{client_timestamp}|{operation}|{source_service}|{session_id}|{payload_hash}
```

- `operation` is `ingest` or `retrieve`.
- `payload_hash` = `sha256(canonical_payload_json(payload))`, where the payload
  subset is `{role, content}` for ingest and `{query}` plus `{top_k}` (only when
  present) for retrieve.
- `canonical_payload_json` = JSON with `sort_keys=True`,
  `separators=(",", ":")`, `ensure_ascii=False`. `None` attribution fields
  render as the empty string `""`.

Memory verifies the signature **constant-time** and checks `client_timestamp`
against a freshness window (`MEMORY_REPLAY_WINDOW_SECONDS`, default 300s) to
bound replay. An invalid/missing signature **or** a stale timestamp returns
`401` with a generic `Invalid or missing signature` detail; the specific reason
(bad signature vs. replay skew) is logged server-side only. The canonical string
matches `careeragent-api/src/client/memory.py` byte-for-byte, the same
signed-boundary discipline as the api → logger boundary.

### 2.1 POST /ingest

Embed one turn and store its vector.

**Body**

Carries the signed envelope (2.0) plus:

| Field | Type | Required | Notes |
|---|---|---|---|
| `session_id` | string ≤128 | yes | Scopes the turn; retrieval filters on it. Signed. |
| `role` | enum string | yes | `user` \| `assistant`. Signed (in the payload subset). |
| `content` | string ≥1 | yes | The turn text to embed and store. Signed (in the payload subset). |

The signed payload subset is `{role, content}`.

**Success — `201 Created`**

```json
{ "session_id": "cf81...d", "role": "user", "stored": true, "duplicate": false, "id": "…uuid…" }
```

`stored:false, duplicate:true` means an identical `(session_id, content_hash)` already existed and the insert was a no-op; `id` is the existing row.

**Errors**

| Status | Cause |
|---|---|
| `401` | Missing/invalid `X-API-Key`, **or** invalid/missing HMAC signature, **or** `client_timestamp` outside the replay window (generic `Invalid or missing signature` detail) |
| `422` | Body fails validation (missing field, bad `role`, empty `content`, malformed envelope) |
| `503` | Embedding unavailable (cold/unreachable/`EMBEDDING_MODEL_URL` unset) — **turn not stored** |
| `500` | Store write failed |

Ingest deliberately does **not** fail open: a silently dropped ingest would remove a turn from all future retrieval with no signal. The `503` lets careeragent-api log/retry off the user's path.

### 2.2 POST /retrieve

Rank a session's stored turns against the current query.

**Body**

Carries the signed envelope (2.0) plus:

| Field | Type | Required | Notes |
|---|---|---|---|
| `session_id` | string ≤128 | yes | Only this session's turns are searched. Signed. |
| `query` | string ≥1 | yes | The current user message; embedded and compared. Signed (in the payload subset). |
| `top_k` | int 1..100 | no | Defaults to `MEMORY_TOP_K_DEFAULT` (5). Signed when present. |

The signed payload subset is `{query}` plus `{top_k}` only when `top_k` is supplied.

**Success — `200 OK`** (always 200)

```json
{
  "session_id": "cf81...d",
  "retrieved": [
    { "id": "…", "role": "assistant", "content": "…", "score": 0.83, "created_at": "2026-05-15T19:53:14.123456+00:00" }
  ],
  "degraded": false
}
```

| Field | Type | Notes |
|---|---|---|
| `retrieved[].score` | float | Cosine similarity (1 − cosine distance), descending. |
| `retrieved[].created_at` | string\|null | ISO-8601 UTC. |
| `degraded` | bool | `true` when retrieval failed open anywhere on the hot path — the embedding call **or** the store search (DB error, dimension mismatch); list is empty. |

**Errors**

| Status | Cause |
|---|---|
| `401` | Missing/invalid `X-API-Key`, **or** invalid/missing HMAC signature, **or** `client_timestamp` outside the replay window (generic `Invalid or missing signature` detail) |
| `422` | Body fails validation (including malformed envelope) |

A failure anywhere on the retrieve hot path — an embedding outage **or** a store-search failure (DB hiccup, embedding-dimension mismatch) — is **not** an error here: it returns `200` with `degraded:true`. A successful retrieve that finds nothing relevant returns `200` with an empty list and `degraded:false`.

### 2.3 GET /health

**Success — `200 OK`**

```json
{
  "status": "ok",
  "careeragent_memory": { "version": "0.1.0", "store": "connected" },
  "careeragent_infra_embed": { "url": "http://careeragent-infra:8002/health", "status": "ok" }
}
```

`status` is `ok` iff memory's store is reachable, else `unhealthy`. `careeragent_infra_embed.status` mirrors careeragent-infra's reported `embedding` field (`ok` / `not configured` / `unreachable`) and is **informational only** — because memory fails open, an unreachable embedder does not make memory unhealthy.

### 2.4 GET /

Unauthenticated identification: `{ "service": "careeragent-memory", "version": "0.1.0" }`. For platform probes that cannot pre-share the key.

---

## 3. Outbound contracts (consumed)

### 3.1 POST {CAREERAGENT_INFRA_URL}/embed

```text
POST /embed
Content-Type: application/json
X-API-Key: <INFRA_API_KEY>

{ "input": "single string" }            # query embed (retrieve)
{ "input": ["turn a", "turn b"] }        # batch embed (future warm-up; one round-trip)
```

Response is careeragent-infra's OpenAI-compatible embeddings JSON; memory reads `data[i].embedding` (ordered by `index`). A non-200 (including `503` "not configured") or a transport timeout becomes an `InfraEmbedError`, which retrieval treats as fail-open and ingest surfaces as `503`.

**Timeouts:** connect `MEMORY_EMBED_CONNECT_TIMEOUT` (5s), read `MEMORY_EMBED_TIMEOUT` (10s). The read timeout is bounded (unlike careeragent-api's `/chat` boundary) precisely so a cold embedder fails open rather than hanging a retrieval.

### 3.2 GET {CAREERAGENT_INFRA_URL}/health

Polled only for the informational `careeragent_infra_embed` field of memory's own `/health`. Never raises; any failure maps to `unreachable`.

### 3.3 Database

The memory-owned Postgres (`memory-db`). Async SQLAlchemy 2.0 over psycopg 3 (`postgresql+psycopg://`), `pool_pre_ping=True`, `pool_recycle=300`.

---

## 4. Store schema

`careeragent_memory.turns` (DDL in `database/init.sql`):

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID` PK | `gen_random_uuid()` default (PG core). |
| `session_id` | `TEXT` | Indexed; every query filters on it. |
| `role` | `TEXT` | `CHECK (role IN ('user','assistant'))`. |
| `content` | `TEXT` | Raw turn text (no PII stripping). |
| `content_hash` | `TEXT` | SHA-256 of `content`; dedupe key. |
| `embedding` | `VECTOR` | **Unsized** — dimensionality follows the embedder; exact search, no ANN index. |
| `created_at` | `TIMESTAMPTZ` | `now()` default. |

Indexes: `idx_turns_session (session_id)`; `uq_turns_session_hash (session_id, content_hash)` UNIQUE (backs `ON CONFLICT DO NOTHING` dedupe).

Search: filtered by `session_id`, score = `1 - (embedding <=> :q)`. The `MEMORY_MIN_SCORE` floor is applied in-database **before** the limit — `WHERE embedding <=> :q <= 1 - :min_score` is evaluated first, then `ORDER BY embedding <=> :q LIMIT :k`. Flooring before the limit (rather than post-filtering the already-limited rows) keeps the floor from eating into the `top_k` budget.

---

## 5. State model

| State | Lifetime | Notes |
|---|---|---|
| `InfraClient` (httpx.AsyncClient) | Process | `INFRA_API_KEY` pre-attached; bounded timeouts. |
| `Store` (async engine + pool) | Process | One async SQLAlchemy engine. |
| API-key validator | Process | Configured at lifespan startup from `MEMORY_API_KEY`. |
| HMAC secret | Process | Configured at lifespan startup from `MEMORY_HMAC_SECRET`; refuses to boot if unset. |

No per-request in-memory state. **All durable state is in the memory-owned Postgres.** A restart loses nothing — turns persist (this is why the write path is `/ingest`, not a payload-rebuilt in-memory cache).

---

## 6. Integration notes for careeragent-api

- Hold `MEMORY_API_KEY` (transport) **and** `MEMORY_HMAC_SECRET` (envelope signing) plus `MEMORY_URL`; send the key as `X-API-Key` and sign every `/retrieve`/`/ingest` body per the canonical string in 2.0. Both secrets must match memory's. The signing implementation lives in `careeragent-api/src/client/memory.py`.
- Per `/chat` turn: `POST /retrieve {session_id, query, top_k}` **before** building the prompt; build `[bio.txt] + [retrieved] + [recent N] + [current]`; after the stream completes, `POST /ingest` the user `input_text` and the assistant `output_text` (already computed for the logger capture). Retrieve is on the hot path (awaited); ingest is off the user's path.
- If `/retrieve` returns `degraded:true`, skip the retrieved block and forward recent turns only.
- `session_id` is supplied by the frontend (minted once per session, resent each turn) and threaded through careeragent-api — the same value can now populate the logger's previously-null `session_id` field.
- `INFRA_API_KEY` here is the same value careeragent-api already holds; memory is a second caller of `/embed`, which careeragent-infra's datasheet anticipates.

---

## 7. Failure modes

| Failure | Detection | Behaviour | Operator signal |
|---|---|---|---|
| Embedder cold/slow | `httpx.TimeoutException` past `MEMORY_EMBED_TIMEOUT` | Retrieve → `200 degraded:true`; Ingest → `503` | `WARNING … embedding unavailable` |
| Embedder unreachable / `EMBEDDING_MODEL_URL` unset | non-200 / 503 from `/embed` | Same as above | `WARNING … /embed returned HTTP 503` |
| DB connection lost / dimension mismatch | `pool_pre_ping` / query error in `store.search` | Retrieve → `200 degraded:true` (fails open); Ingest → `500`; `/health` → `unhealthy` | `WARNING … store search failed` |
| Duplicate ingest | `(session_id, content_hash)` conflict | `201 stored:false duplicate:true` (no-op) | `INFO … duplicate` |
| Invalid/missing key | `require_api_key` | `401` | `WARNING … invalid X-API-Key` |
| Invalid/missing HMAC signature | `security.enforce` | `401` (`Invalid or missing signature`) | `WARNING … Invalid HMAC signature` |
| Stale/skewed `client_timestamp` | `security.enforce` (replay window) | `401` (`Invalid or missing signature`) | `WARNING … client_timestamp outside replay window` |
| Malformed body | Pydantic | `422` | — |

Throughout, `/chat` (in careeragent-api) is unaffected by retrieval-side failures — retrieval degrades, it does not block.

---

## 8. Operational characteristics (expectations, not guarantees)

| Property | Expected |
|---|---|
| `/retrieve` latency (warm embedder) | dominated by the `/embed` round-trip + one indexed SQL scan |
| `/ingest` latency (warm embedder) | one `/embed` round-trip + one INSERT |
| Image size | ~180–220 MB (pure Python + psycopg) |
| Cold-start tax | first `/embed` after the embedder scales to zero (bounded by `MEMORY_EMBED_TIMEOUT`) |

---

## 9. Version history

| Version | Notes |
|---|---|
| 0.1.0 | Initial release. `POST /retrieve` + `POST /ingest`, memory-owned Postgres + pgvector, exact session-scoped cosine search, transport-key auth, fail-open retrieval / confirmed-write ingest. Pre-production. |
| 0.1.0 (HMAC) | Added HMAC-SHA256 signed-envelope integrity + replay protection on `/retrieve` and `/ingest` (second secret `MEMORY_HMAC_SECRET`, replay window `MEMORY_REPLAY_WINDOW_SECONDS`). Reverses the earlier transport-key-only / no-HMAC stance; the api → memory boundary now matches the api → logger signed-boundary discipline. |

---

*careeragent-memory — part of the CareerAgent system*
