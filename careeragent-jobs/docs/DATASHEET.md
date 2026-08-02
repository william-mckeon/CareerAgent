# careeragent-jobs — Datasheet

> Reference document for building on top of careeragent-jobs.
> Audience: **careeragent-api** (the enqueuer) and any operator wiring the stack.

---

## Quick Reference

| Item | Value |
|---|---|
| Role | Background/async job runner for CareerAgent (P7 #18a) |
| Base URL | `http://localhost:8011` |
| Position | `careeragent-api → careeragent-jobs → careeragent-review (+ careeragent-sessions inject)` |
| Auth in (caller) | `X-API-Key` = `JOBS_API_KEY` (required on all but `/health`) |
| Auth out (review) | `X-API-Key` = `REVIEW_API_KEY` |
| Auth out (sessions) | `X-API-Key` = `SESSIONS_API_KEY` |
| Store | Own Postgres, schema `careeragent_jobs` (one `jobs` table) |
| Worker | In-process asyncio task (poll → claim → run → inject) |
| Port | `8011` |
| Version | 0.1.0 |

---

## Overview

`careeragent-jobs` runs slow tasks off the request path. `careeragent-api` enqueues a job and gets
an id back instantly; the worker claims it, runs it (calling a leaf service directly — no model, no
agent loop), stores the result, and injects the result as an assistant message into the job's
conversation via `careeragent-sessions`. The user sees the answer appear — no polling.

This slice ships one job kind, `review_repos` (a repo-review fan-out via `careeragent-review`).
`careeragent-api` is **not modified** by this service; it simply gains a `JobsClient` that calls
`POST /jobs`.

---

## Authentication

- **Inbound** `X-API-Key` = `JOBS_API_KEY` (careeragent-api ↔ jobs). Constant-time compare, read at
  call time; `/health` is unauthenticated.
- **Outbound** `REVIEW_API_KEY` → careeragent-review, `SESSIONS_API_KEY` → careeragent-sessions.
  Separate boundary keys — never reuse the inbound key.

---

## API Reference

### `POST /jobs` — `X-API-Key`
Request:
```json
{ "kind": "review_repos", "spec": { "repos": ["owner/name"], "limit": 10, "focus": "backend", "force": false }, "conversation_id": "<uuid|null>" }
```
- `kind` must be a known kind (`review_repos`) or → `400 {"detail":"unknown job kind '<k>'"}`.
- `spec` is opaque per-kind params (all optional for `review_repos`).
- `conversation_id` optional; when set, the finished result is injected there. Malformed → `400`.

Response `201`:
```json
{ "id": "<uuid>", "status": "pending" }
```
Errors: `400` unknown kind / bad UUID · `401` bad key · `503` store unavailable.

### `GET /jobs/{id}` — `X-API-Key`
```json
{ "id": "...", "kind": "review_repos", "status": "pending|running|done|failed",
  "attempts": 1, "result": "…summary…"|null, "error": null|"…",
  "conversation_id": "…"|null, "created_at": "…", "updated_at": "…" }
```
`404` if unknown. `spec` is intentionally not returned.

### `GET /jobs` — `X-API-Key`
Query: `conversation_id` (uuid), `status` (pending|running|done|failed), `limit` (1–100, default 100).
Returns the same fields as above, **newest first**. None-valued filters are dropped.

### `GET /health` — no auth
```json
{ "status": "ok|degraded", "service": "careeragent-jobs", "database": "ok|unreachable" }
```
`status` is `ok` when the database is reachable.

---

## Job lifecycle

```
pending ──claim_one()──▶ running ──handler ok──▶ done   (+ inject into conversation)
   ▲                         │
   └──retry_or_fail (attempts<max)   └──handler error──▶ (attempts==max) failed
```
`attempts` is incremented at claim time. With `JOBS_MAX_ATTEMPTS=3` a failing job runs at attempts
1, 2, 3 and is then marked `failed`.

---

## Store

Schema `careeragent_jobs` (all objects schema-qualified — keeps the shared-instance switch config-only):
```
jobs(id uuid pk default gen_random_uuid(), kind text, spec jsonb, conversation_id uuid,
     status text, attempts int, result text, error text, created_at, updated_at)
index jobs_claimable       on jobs (created_at) where status='pending'
index jobs_by_conversation on jobs (conversation_id, created_at desc)
```
Claim uses `FOR UPDATE SKIP LOCKED` so multiple workers/replicas never double-run a job. `init.sql`
creates the table on first DB boot; `store.ensure_schema()` also creates it idempotently at startup.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `JOBS_API_KEY` | — | Inbound X-API-Key. Required. |
| `SESSIONS_URL` | `http://careeragent-sessions:8005` | Where finished results are injected. |
| `SESSIONS_API_KEY` | — | Outbound key (matches sessions' inbound). Required for injection. |
| `REVIEW_URL` | `http://careeragent-review:8007` | Where `review_repos` runs the fan-out. |
| `REVIEW_API_KEY` | — | Outbound key (matches review's inbound). Required for review jobs. |
| `JOBS_DB_USER` | `careeragent_jobs` | DB role. |
| `JOBS_DB_PASSWORD` | — | DB password. Required. |
| `JOBS_DB_HOST` | `jobs-db` | DB host (compose sets this). |
| `JOBS_DB_PORT` | `5432` | DB port. |
| `JOBS_DB_NAME` | `careeragent_jobs` | DB name. |
| `JOBS_DB_SCHEMA` | `careeragent_jobs` | Schema for all objects. |
| `JOBS_DATABASE_URL` | — | Optional full URL (overrides parts). |
| `JOBS_WORKER_POLL_SECONDS` | `2` | Worker poll interval when the queue is empty. |
| `JOBS_MAX_ATTEMPTS` | `3` | Per-job attempt cap before `failed`. |
| `JOBS_PORT` | `8011` | Listen port. |
| `JOBS_ENABLE_DOCS` | `false` | Expose `/docs` when `true`. |

---

## Known Behaviors

| Behavior | Note |
|---|---|
| Result delivered by injection, not polling | Worker POSTs the summary to sessions' `/conversations/{id}/inject` |
| Injection is best-effort | Job is marked `done` before injection; a `404`/failure is logged, result still stored |
| Single in-process worker | Claim is `SKIP LOCKED`-safe, so adding workers later is a config change |
| Only `review_repos` in v0.1.0 | The registry (`jobtypes.HANDLERS`) makes new kinds a local change |
| No scheduler/cron | Recurring jobs are a later slice (#18b) |

---

## Design Decisions

- **A dedicated service, not a thread in `api`** — slow work gets its own lifecycle and (later)
  scale-out, without holding a request worker.
- **Deliver via `sessions` injection** — the transcript is the system of record; the result appears
  where the user already is. "Do not poll."
- **`FOR UPDATE SKIP LOCKED` from day one** — safe multi-worker claim for free.
- **Own DB now, shared later** — schema-qualified + env-driven → config-only switch.

## Non-goals (v0.1.0)

- A **scheduler/cron** for recurring jobs (later slice).
- **More job kinds** beyond `review_repos`.
- A **live push channel** to the frontend (delivery is via the transcript).

---

*careeragent-jobs — part of the CareerAgent system. Port 8011.*
