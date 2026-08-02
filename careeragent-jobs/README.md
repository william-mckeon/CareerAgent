# careeragent-jobs

> **The background/async job runner for CareerAgent** — runs slow tasks off the request path and
> injects the result into the conversation when done. "Do not poll."

---

## Overview

`careeragent-jobs` is the async-work layer of the CareerAgent system (P7 #18a). `careeragent-api`
**enqueues** a slow task via `POST /jobs` and gets an id back instantly; an in-process **worker**
claims it, runs it, stores the result, and **injects the result as an assistant message** into the
job's conversation via `careeragent-sessions` — so it simply appears, with no client polling.

It exists because some capabilities are too slow for a chat turn — a repo-review fan-out over the
user's GitHub can take minutes. Running it inline blocks or times out the turn; making the user poll
is a poor experience. `jobs` fixes exactly that gap — and nothing more. It is a **new service that
changes no existing service's code**; `careeragent-api` simply gains a `JobsClient`.

There is **no agent loop and no model** here. The worker calls leaf services directly. This slice
ships one job kind, `review_repos` (via `careeragent-review`). A scheduler/cron for recurring jobs
is a later slice.

---

## Where This Fits

```text
careeragent-api ──POST /jobs──▶ careeragent-jobs (:8011) ──┬─▶ careeragent-review  (:8007)  the work
                                (worker, own Postgres)      └─▶ careeragent-sessions (:8005)  inject result
```

**Port convention:** 8000 frontend · 8001 api · 8005 sessions · 8007 review · **8011 jobs**.

---

## Architecture

```text
┌─────────────────────────────────────────────────┐
│  careeragent-jobs  (port 8011)                    │
│  FastAPI — src/backend/api.py                     │
│                                                   │
│  POST /jobs        → enqueue (return id instantly)│
│  GET  /jobs/{id}   → status / result              │
│  GET  /jobs        → list (by conversation/status)│
│  GET  /health      → db status                    │
│                                                   │
│  worker (src/worker.py) — asyncio task            │
│    claim_one → run handler → finish → inject      │
└───────┬───────────────────────────┬───────────────┘
        │ handler calls (httpx)      │ persist (asyncpg)
        ▼                            ▼
  careeragent-review (:8007)     careeragent-jobs-db (Postgres)
  careeragent-sessions (:8005)   schema: careeragent_jobs
```

### Job flow
1. `careeragent-api` → `POST /jobs {kind, spec, conversation_id}` → `201 {id, status:"pending"}`.
2. The worker `claim_one()`s the oldest pending job atomically (`FOR UPDATE SKIP LOCKED`).
3. It dispatches on `kind` to a handler (`src/jobtypes.py`), which calls the leaf service.
4. On success it stores the summary and injects it into the conversation via `sessions`.
5. On a handler error it re-queues up to `JOBS_MAX_ATTEMPTS`, then marks the job `failed`.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Base image | `python:3.11-slim` |
| API | FastAPI + uvicorn (async) |
| Persistence | PostgreSQL via SQLAlchemy (async) + asyncpg |
| Outbound | httpx (to careeragent-review and careeragent-sessions) |
| Auth | `X-API-Key` (inbound) + separate outbound keys |
| Containerization | Docker + Docker Compose |

---

## Prerequisites

- Docker Desktop.
- The external network: `docker network create careeragent-network`.
- `careeragent-review` and `careeragent-sessions` reachable on the network.
- A `.env` (from `.env.example`) with `JOBS_API_KEY`, `REVIEW_API_KEY`, `SESSIONS_API_KEY`, and
  `JOBS_DB_PASSWORD`.

---

## Project Structure

```text
careeragent-jobs/
├── docker/jobs/Dockerfile
├── database/
│   ├── init.sql                 # jobs table + indexes, schema careeragent_jobs
│   └── migrations/0001_jobs.sql # the numbered, replayable migration of record
├── src/
│   ├── backend/api.py           # FastAPI app — endpoints + lifespan (starts the worker)
│   ├── worker.py                # the claim→run→inject loop
│   ├── jobtypes.py              # the job-kind registry (HANDLERS) — review_repos
│   ├── store.py                 # jobs persistence (enqueue/claim_one/finish/retry_or_fail)
│   ├── client/review.py         # outbound → careeragent-review /review-batch
│   ├── client/sessions.py       # outbound → careeragent-sessions /conversations/{id}/inject
│   ├── schemas.py               # pydantic models
│   └── security.py              # X-API-Key auth
├── tests/                       # store (live-DB, skips w/o PG), worker, jobtypes, api
├── specs/0001-jobs.md           # the contract
├── docker-compose.yml           # jobs + its Postgres, on careeragent-network
├── requirements.txt / requirements-dev.txt
├── .env.example
└── docs/DATASHEET.md
```

---

## Setup

```bash
cp .env.example .env            # set JOBS_API_KEY, REVIEW_API_KEY, SESSIONS_API_KEY, JOBS_DB_PASSWORD
docker network create careeragent-network   # if not already created
docker compose up -d --build
curl http://localhost:8011/health
```

---

## API Reference (summary)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/jobs` | X-API-Key | Enqueue a job; returns `{id, status:"pending"}` |
| `GET` | `/jobs/{id}` | X-API-Key | Job status/result (404 if unknown) |
| `GET` | `/jobs` | X-API-Key | List jobs (newest first; filter by `conversation_id`/`status`) |
| `GET` | `/health` | none | `{status, service, database}` |

`POST /jobs` body: `{ "kind": "review_repos", "spec": {…}, "conversation_id": "<uuid|null>" }`.
Only `review_repos` is a valid kind in this slice. See `docs/DATASHEET.md` for full shapes.

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `JOBS_API_KEY` | — | Inbound X-API-Key (api ↔ jobs). Required. |
| `SESSIONS_URL` / `SESSIONS_API_KEY` | `http://careeragent-sessions:8005` / — | Result injection target + key. |
| `REVIEW_URL` / `REVIEW_API_KEY` | `http://careeragent-review:8007` / — | Review fan-out target + key. |
| `JOBS_DB_USER/PASSWORD/HOST/PORT/NAME` | see `.env.example` | DB connection parts. |
| `JOBS_DB_SCHEMA` | `careeragent_jobs` | Schema for all objects — keeps the shared-instance switch config-only. |
| `JOBS_DATABASE_URL` | — | Optional full URL, overrides the parts. |
| `JOBS_WORKER_POLL_SECONDS` | `2` | Worker poll interval when the queue is empty. |
| `JOBS_MAX_ATTEMPTS` | `3` | Per-job attempt cap before `failed`. |
| `JOBS_PORT` | `8011` | Listen port. |
| `JOBS_ENABLE_DOCS` | `false` | Expose `/docs` when `true`. |

---

## Testing

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -q
```
The pure-logic, worker, jobtypes, and API tests are hermetic (no DB, no network). The `store`
round-trip tests need a live Postgres and **skip** when none is reachable; point `JOBS_DATABASE_URL`
(or the `JOBS_DB_*` parts) at a throwaway DB to run them (they TRUNCATE `jobs` for isolation).

---

## Design Decisions

- **A dedicated service, not a thread in `api`.** Slow work gets its own lifecycle and (later)
  scale-out without holding a request worker or coupling to the gateway.
- **Deliver via `sessions` injection, not a status the client polls.** The transcript is the system
  of record; the result appears where the user already is.
- **`FOR UPDATE SKIP LOCKED` from day one.** A safe multi-worker claim for free, even though this
  slice runs a single worker.
- **Own DB now, shared later — for free.** Everything is schema-qualified to `careeragent_jobs` and
  the connection is env-driven, so pointing at a shared Postgres instance is config-only.

---

## License

Apache License 2.0 (see the repository root `LICENSE`).

## Maintainer

**William McKeon**
