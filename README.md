# careeragent-os

> The umbrella for the CareerAgent system — a version-pinned manifest of which commit
> of each service runs together, plus the runbook for bringing the whole thing
> up by hand. It builds nothing and owns nothing: every service runs from its
> own repo, with its own Docker setup and its own `.env`.

**Maintainer:** William McKeon ([github.com/william-mckeon](https://github.com/william-mckeon))  ·  **Status:** actively maintained  ·  Apache 2.0 License © 2026 William McKeon

> **Roadmap.** The coach's evolution into a persistent, question-asking agent is planned in **[`ROADMAP.md`](ROADMAP.md)** — 20 capability gaps grouped into 7 dependency-ordered phases, with per-phase design specs in [`careeragent-api/specs/`](careeragent-api/specs/). The plan is documented before coding to prevent drift.

---

## What this is

**CareerAgent** is a decoupled, multi-service reference implementation of a stateful AI agent — a set of small, single-responsibility services that together stand up a working agent. I built it to get the infrastructure right: clean boundaries between services, compartmentalized auth across every hop, and structured capture of what happens. Each service lives in its own repo with its own Docker setup and its own `.env`, and each is deployable on its own.

The system is **four core services plus one optional fifth.** The four core services (frontend, api, infra, logger) stand up a complete working agent. The fifth, **careeragent-memory**, is an opt-in session-scoped retrieval layer (RAG); enable it for conversation memory, or leave it out entirely and the rest of the system is unaffected.

`careeragent-os` is the umbrella over those services, and it is deliberately thin. It contains **no product code, no compose file, and no secrets.** It holds each service as a **git submodule** pinned to a specific commit, and it carries this README — the project's explanation and the runbook for standing the whole system up by hand.

It runs nothing and owns nothing. Each service is brought up from its own folder, with its own `docker compose` and its own `.env`, exactly as that repo was designed. The shared `careeragent-network` and the shared Postgres are owned by **careeragent-logger** — its setup scripts create the network and its README brings up the database. `careeragent-os` simply pins the versions and documents the order they come up in.

---

## The system at a glance

```text
User
  │  http://localhost:8000
  ▼
careeragent-frontend (:8000)        Streamlit chat UI
  │
  ▼
careeragent-api (:8001)             Identity gateway — persona, auth, prompt assembly, SSE relay, event emission
  ├──────────────▶ careeragent-infra  (:8002) ──▶ BYOC Provider   (hot path: the response)
  ├──────────────▶ careeragent-memory (:8004) ──▶ pgvector        (OPTIONAL: retrieve before, ingest after)
  └──────────────▶ careeragent-logger (:8003) ──▶ Postgres        (side path: fire-and-forget capture)
```

Ports: **8000** frontend (user-facing), **8001** careeragent-api, **8002** careeragent-infra, **8003** careeragent-logger, **8004** careeragent-memory (optional), **5432** shared Postgres. Only 8000 is meant for users; the rest are internal to the stack. `careeragent-memory`, when enabled, owns its **own** PostgreSQL + pgvector instance (separate from the shared Postgres) and reaches `careeragent-infra` for embeddings.

---

## How it's put together

A few principles shaped the design:

* **Identity off the client.** `careeragent-frontend` is a pure UI shell. The persona and agent logic live upstream in `careeragent-api`, so any client — web, mobile, CLI — inherits one canonical agent instead of re-implementing it.
* **A robust identity gateway.** `careeragent-api` acts as the traffic cop: it owns the persona, the compartmentalized auth chain, the prompt assembly, and the streaming relay. It is where CareerAgent stops being a UI idea and becomes a backend service.
* **Cryptographically verified capture.** `careeragent-logger` is the capture layer. `careeragent-api` emits a structured event stream and full conversation captures on every turn: append-only, HMAC-signed, retention-managed.
* **A reasoning model, a control model, and an embedding model.** `careeragent-infra` proxies requests to three models — a **base model** for deep reasoning and everyday conversation, a **nervous-system model** as the fast control layer for routing, history filtering, and agent decisions, and an **embedding model** that turns text into vectors for retrieval. Deep work, quick decisions, and semantic lookup each handled by the right tool.
* **Optional session memory.** `careeragent-memory` is an opt-in retrieval layer (RAG). It embeds each conversation turn into its own pgvector store and ranks the most relevant prior turns back into the prompt. The gateway assembles the prompt; memory only ranks. Retrieval is fail-open on the hot path, so the agent runs the same with or without it — memory is an enhancement, never a hard dependency.

---

## Service status

| Service | Submodule | Version | Status | Role |
| --- | --- | --- | --- | --- |
| careeragent-frontend | `careeragent-frontend/` | 1.0.0 | working | Streamlit chat UI |
| careeragent-api | `careeragent-api/` | 1.0.0 | working | Identity gateway: persona, auth, prompt assembly, SSE relay, event emission (+ optional memory retrieve/ingest) |
| careeragent-infra | `careeragent-infra/` | 1.0.0 | working | Model proxy → BYOC Provider (base reasoning + nervous-system control layer + embedding) |
| careeragent-logger | `careeragent-logger/` | 0.1.0 | working (pre-production) | Capture layer: ops events, conversation captures, audit; owns the network + shared Postgres |
| careeragent-memory | `careeragent-memory/` | 0.1.0 | working (pre-production) · **optional** | Session-scoped RAG layer: embeds and ranks prior turns; owns its own PostgreSQL + pgvector |

---

## Architecture

The diagram below shows the **core four-service system** — the two always-present boundaries off `careeragent-api`: the `careeragent-infra` hot path and the `careeragent-logger` fire-and-forget sibling. The optional `careeragent-memory` boundary is documented separately, just below, so the core diagram stays readable.

```text
                            Browser  →  http://localhost:8000
                                          │
                                          ▼
                              ┌──────────────────────────┐
                              │ careeragent-frontend :8000 │  Streamlit UI
                              └───────────┬──────────────┘
                                          │  X-API-Key: CAREERAGENT_API_KEY
                                          ▼
                              ┌──────────────────────────┐
                              │ careeragent-api      :8001 │  persona · auth · prompt assembly · SSE relay
                              └─────┬───────────────┬────┘
                  HOT PATH          │               │   FIRE-AND-FORGET
       X-API-Key: INFRA_API_KEY     │               │   X-API-Key: LOGGER_API_KEY + HMAC
                                    ▼               ▼
                    ┌─────────────────────┐  ┌─────────────────────┐
                    │ careeragent-infra:8002│  │careeragent-logger:8003│
                    │ model proxy         │  │capture layer        │
                    └─────────┬───────────┘  └─────────┬───────────┘
                Bearer PROVIDER_API_KEY                │ PostgreSQL
                              ▼                        ▼
                    ┌─────────────────────┐  ┌─────────────────────┐
                    │ BYOC Provider       │  │ careeragent-shared-db │
                    │ base model          │  │ schema              │
                    │ nervous-system      │  │ careeragent_logger    │
                    │ embedding           │  │                     │
                    └─────────────────────┘  └─────────────────────┘
```

* **Hot path:** frontend → careeragent-api → careeragent-infra → Compute Provider. This must succeed for the user to get a response.
* **Side path:** careeragent-api → careeragent-logger is fire-and-forget — if the logger is down, `/chat` is unaffected; events queue and are dropped per the overflow policy.
* **One network** (`careeragent-network`), created and owned by **careeragent-logger** (its `scripts/setup-network.*`). Every service attaches to it and addresses the others by name once they're all on it.
* **Shared Postgres** (`careeragent-shared-db`), also brought up via **careeragent-logger**'s setup, hosts the `careeragent_logger` schema.

### Optional: careeragent-memory (session retrieval)

When enabled, `careeragent-api` gains a third outbound boundary to `careeragent-memory` (:8004). It is consulted twice per turn — once on the hot path (retrieve, before prompt assembly) and once off the user's path (ingest, after a clean stream):

```text
   careeragent-api (:8001)
     │
     ├─[hot path, before assembly]──▶ careeragent-memory  POST /retrieve   (fail-open, bounded; never blocks the first token)
     │
     └─[off path, after the answer]─▶ careeragent-memory  POST /ingest ×2  (signalled on failure, never blocks /chat)

   careeragent-memory (:8004)  —  session-scoped RAG, OPTIONAL
     │   Auth in:  X-API-Key: MEMORY_API_KEY   (transport-key only — NO HMAC today)
     │
     ├──▶ careeragent-infra  POST /embed         (X-API-Key: INFRA_API_KEY — turns text into vectors)
     └──▶ memory-owned PostgreSQL + pgvector   (its OWN database, NOT the shared careeragent-shared-db)
```

* **Optional / opt-in.** Memory is enabled only when `careeragent-api` is configured for it (`MEMORY_URL` + `MEMORY_API_KEY`). Absent that configuration, the gateway forwards full history exactly as before and never calls memory. It is never a refuse-to-boot dependency.
* **Memory ranks; the gateway builds.** `careeragent-memory` returns ranked prior turns; `careeragent-api` decides what actually goes into the prompt (`[bio] + [retrieved older turns] + [recent N turns] + [current turn]`), so the retrieval layer can be swapped without touching prompt policy.
* **Its own store.** `careeragent-memory` owns its own PostgreSQL + pgvector instance for the conversation turns — distinct from the logger's shared Postgres. That database holds conversation content at rest; treat it with the same care as the logger's captures.
* **Reached only by careeragent-api**, and only when enabled. It is never called from a browser.

---

## Security model (summary)

Authentication between services is **compartmentalized** — every boundary has its own independent secret, and no key is ever relayed unchanged to the next hop:

```text
frontend ──CAREERAGENT_API_KEY──▶ careeragent-api ──INFRA_API_KEY──▶ careeragent-infra ──PROVIDER_API_KEY──▶ BYOC Provider
                               ├──LOGGER_API_KEY (+ LOGGER_HMAC_SECRET signing)──▶ careeragent-logger
                               └──MEMORY_API_KEY (transport only, no HMAC)──▶ careeragent-memory ──INFRA_API_KEY──▶ careeragent-infra (/embed)
```

A single-service compromise is bounded to that one boundary. The logger boundary adds payload integrity: every event is HMAC-signed and the signature is stored on the row, so captures can be re-verified offline. The **memory boundary is transport-key only today** — `careeragent-memory` uses `X-API-Key: MEMORY_API_KEY` and defines no HMAC contract yet (the client scaffolds signing for a future addition), so it has wire-access control but no offline payload-integrity proof for stored turns. Full detail lives in the security sections of careeragent-api, careeragent-logger, and careeragent-memory.

There is **no central `.env`** — each secret lives in the `.env` of the service that uses it, and the shared values must match on both sides of every boundary. Two naming/sharing wrinkles to know:

* The api ↔ careeragent-infra secret is the same value on both ends, but it is called `INFRA_API_KEY` in careeragent-api and `API_KEY` in careeragent-infra. When memory is enabled, **careeragent-memory also holds that same value** (as `INFRA_API_KEY`) because it is a second caller of careeragent-infra's `/embed`.
* `MEMORY_API_KEY` is a separate, independent value shared only between careeragent-api and careeragent-memory. careeragent-memory also provisions its **own** database password for its own Postgres, internal to its stack.

---

## Bringing the system up

This is a manual, directory-by-directory bring-up. `careeragent-os` does not drive it — each service is started from its own folder, with its own `.env` and its own `docker compose`, exactly as that repo documents. What follows is the **order** and the **wiring context**; for the specifics of any one service, follow that repo's own README.

**Prerequisites:** Docker; your choice of BYOC provider endpoints (e.g., RunPod serverless, OpenAI-compatible APIs) for the base and nervous-system models, plus an embedding endpoint **if you enable careeragent-memory**; and the submodule folders present (four core, plus `careeragent-memory` if you want it).

**0. Get the folders.** Clone careeragent-os with its submodules (or initialize them after a plain clone):

```bash
git clone --recurse-submodules https://github.com/william-mckeon/careeragent-os.git
cd careeragent-os
# or, after a plain clone:
git submodule update --init --recursive
```

**1. Shared network + Postgres — owned by careeragent-logger.** The `careeragent-network` and the shared Postgres (`careeragent-shared-db`) belong to careeragent-logger, not careeragent-os. Following **careeragent-logger's own README**, do this first:

* create the network (its `scripts/setup-network.*`, equivalently `docker network create careeragent-network`), and
* bring up the shared Postgres — careeragent-logger's setup mounts its `database/init.sql` and passes the `careeragent_logger` role password to Postgres via `PGOPTIONS`, so the role and schema are provisioned on first boot.

Everything below attaches to that network and that database, so it has to exist first.

**2. careeragent-logger (:8003)** — the capture layer. Needs Postgres healthy first.

```bash
cd careeragent-logger
cp .env.example .env        # then fill it in per careeragent-logger's README
docker compose up -d --build
cd ..
```

**3. careeragent-infra (:8002)** — the model proxy. Independent of the other services.

```bash
cd careeragent-infra
cp .env.example .env        # fill in per careeragent-infra's README
docker compose up -d --build
cd ..
```

**4. careeragent-memory (:8004) — OPTIONAL.** The session-scoped retrieval layer (RAG). **Skip this entire step to run without memory.** Unlike the other services, it brings up its **own** PostgreSQL + pgvector (not the shared Postgres). It calls careeragent-infra's `/embed`, so bring careeragent-infra up first.

```bash
cd careeragent-memory
cp .env.example .env        # fill in per careeragent-memory's README;
                            # set its INFRA_API_KEY to match careeragent-infra's API_KEY
docker compose up -d --build
cd ..
```

**5. careeragent-api (:8001)** — the identity gateway. Calls careeragent-infra (hot path) and careeragent-logger (fire-and-forget), plus careeragent-memory when enabled, so bring those up first. If you ran step 4, also set `MEMORY_URL` + `MEMORY_API_KEY` (and `MEMORY_SESSION_ID`) in careeragent-api's `.env`; leave them unset to run without memory.

```bash
cd careeragent-api
cp .env.example .env        # fill in per careeragent-api's README
docker compose up -d --build
cd ..
```

**6. careeragent-frontend (host :8000)** — the UI. Talks only to careeragent-api.

```bash
cd careeragent-frontend
cp .env.example .env        # fill in per careeragent-frontend's README
docker compose up -d --build
cd ..
```

**7. Open the UI:** http://localhost:8000

> **Secrets must match across boundaries.** With no central `.env`, each repo carries its own — and the shared values have to agree on both sides of every hop: `CAREERAGENT_API_KEY` (frontend ↔ careeragent-api); the api ↔ careeragent-infra value, which is `INFRA_API_KEY` in careeragent-api and the same value as `API_KEY` in careeragent-infra; `LOGGER_API_KEY` + `LOGGER_HMAC_SECRET` (careeragent-api ↔ careeragent-logger); and the `careeragent_logger` DB password (the value you provision Postgres with in step 1 must equal careeragent-logger's `LOGGER_DB_PASSWORD`). **If you enabled memory:** `MEMORY_API_KEY` (careeragent-api ↔ careeragent-memory), and careeragent-memory's `INFRA_API_KEY` must also equal careeragent-infra's `API_KEY` since memory is a second caller of `/embed`. Each repo's `.env.example` documents its own variable names.

> **Reaching each other.** Each repo's `.env` also sets how it addresses its neighbors. With everything on `careeragent-network`, point each service at the others by their container name (e.g. careeragent-api → `http://careeragent-infra:8002`, `http://careeragent-logger:8003`, and when enabled `http://careeragent-memory:8004`; careeragent-memory → `http://careeragent-infra:8002` for `/embed`; frontend → `http://careeragent-api:8001`); the per-repo `.env.example` files list these alongside their standalone (`host.docker.internal`) alternatives.

> **Cold starts.** If you use scale-to-zero serverless endpoints for your BYOC layer, the first call after a quiet period waits for a worker to spin up. Note that `careeragent-infra`'s `/health` reports a reachable-but-cold worker as `ok` — it answers "is the provider reachable?", not "is the model warm?" (it reports `degraded` only when a host genuinely cannot be reached). So the cold-start wait isn't gated away up front; it is absorbed on the first `/chat` (or `/embed`) call, where the read timeout is unbounded. Any "wait until warm" behavior in `careeragent-frontend` therefore can't rely on `/health` alone to detect warmth. The same cold-start applies to the embedding endpoint behind `careeragent-memory`: a cold embedder makes retrieval fail open (recent-only) until it warms, which never fails `/chat`.

---

## Maintaining the system

Each service stays a fully independent repo — its own git, its own CI, its own deploy. `careeragent-os` only records *which commit of each* the system was last known to run together, via the submodule pointers.

```bash
# 1. Work in a service on its own remote, as usual
cd careeragent-api
git commit -am "..."
git push

# 2. Record the new commit in careeragent-os
cd ..
git submodule update --remote --merge careeragent-api   # or: cd careeragent-api && git pull && cd ..
git add careeragent-api
git commit -m "Bump careeragent-api to <short-sha>"
git push
```

Bumping `careeragent-os` is just moving a submodule pointer. `careeragent-os` is a version-controlled manifest of which commits stand up together, plus this README explaining how to stand them up.

---

## Repo layout

```text
careeragent-os/
├── README.md             # this file — the project doc + manual bring-up runbook
├── .gitmodules           # pins each service to a commit (created by `git submodule add`)
├── careeragent-frontend/   # submodule
├── careeragent-api/        # submodule
├── careeragent-infra/      # submodule
├── careeragent-logger/     # submodule — owns the careeragent-network + shared Postgres setup
└── careeragent-memory/     # submodule — OPTIONAL; session-scoped RAG, owns its own Postgres + pgvector
```

No compose file, no `.env`, no Makefile. `careeragent-os` is this README and the submodule pointers; everything that runs lives in the service folders (four core, plus the optional `careeragent-memory`).

---

## Note on the model setup

`careeragent-infra` proxies requests to **three** separate functional models. Two are reasoning models reached over the `/chat` route: a **base model** (for deep reasoning and standard response generation) and a **nervous-system model** (a fast control layer for routing, history filtering, and metadata extraction). `careeragent-api` routes to the base model by default; explicit routing to the control layer is handled via a model override parameter (e.g., `model="nervous_system"`).

The third is an **embedding model**, reached over a separate `/embed` route. It is a different *kind* of model — it turns text into vectors for retrieval rather than generating a reply, so it has no reasoning level and does not stream (it returns a single JSON response). It is optional: when no embedding endpoint is configured, `/embed` reports "not configured" and the `/chat` path is unaffected. When `careeragent-memory` is enabled, it is the consumer of `/embed` — embedding each turn on ingest and each query on retrieval.

All three are reached as OpenAI-compatible endpoints behind the one proxy, which lets you mix and match providers or model sizes to balance compute cost against latency.