# careeragent-dossier

> **The career data system-of-record for CareerAgent** — the master profile and the
> job-application tracker (companies, roles, JDs, tailored resumes, contacts, timeline),
> in a plain relational store with **no vectors**. Its only client is `careeragent-api`.

---

## Overview

`careeragent-dossier` owns the material the resume coach works from and produces:

- **The master profile** — one living document of your career/project history. The *source of
  truth* every tailored resume is rendered from.
- **The application tracker** — one row per opportunity: company, title, job description, the
  tailored `final_resume`, status, timeline (`applied_at` / `last_contact` / `next_follow_up`),
  loose fields (posting URL, location, salary, notes), **contacts** (many per application), and
  a full **resume version history**.

It is deliberately a **dumb data service**: CRUD + lookup, and nothing else. It holds **no agent
logic** — no tool-calling loop, no permission engine, no persona. Those live in
`careeragent-api` (the agent). Dossier is the *nouns*; the agent is the *verbs*. This split keeps
the risky, judgment-heavy parts in one place and leaves this service small and fully testable.

**No vectors, on purpose.** A resume is short, structured, and authoritative — you always want it
*whole*, and you query the tracker by real fields ("still interviewing", "no contact in two
weeks", "applications to fintech"). That is relational work. Lookup is structured filters +
Postgres **full-text search** (stemmed + ranked) + **trigram** fuzzy matching (`pg_trgm`, for
typo-tolerant company/contact names). See [`specs/0001-dossier.md`](specs/0001-dossier.md).

---

## Where this fits

```text
careeragent-api (:8001, the agent)  ── HTTP tool calls ──▶  careeragent-dossier (:8006)  ──▶  Postgres
```

Dossier is a **leaf**: it calls nothing outbound, it is not on the chat path, and it is never
reached by the frontend. `careeragent-api` is its sole client — exactly as `careeragent-memory`
is a client of `api`. Each of dossier's endpoints is one of the agent's data **tools**.

```text
                      ┌─────────────────────────────────────────────┐
   the agent  ─────▶  │  careeragent-dossier  (FastAPI, :8006)       │
   (careeragent-api)  │                                             │
                      │   read_profile / save_profile / edit_profile│
                      │   search_applications / get_application      │
                      │   create/update/delete_application           │
                      │   add_contact                                │
                      │   save_resume / edit_resume                  │
                      └───────────────────────┬─────────────────────┘
                                              │  async SQLAlchemy / asyncpg
                                              ▼
                      ┌─────────────────────────────────────────────┐
                      │  Postgres 16  (schema careeragent_dossier)   │
                      │   profile · applications · contacts ·        │
                      │   resume_versions   + FTS & pg_trgm indexes  │
                      └─────────────────────────────────────────────┘
```

---

## The tool endpoints

Each endpoint is one agent tool. All require `X-API-Key` except `/health`. **Read** endpoints are
the ones the agent is allowed to use in its read-only `plan` mode ("critique, don't touch");
**write** endpoints are unlocked in `acceptEdits` ("go ahead and change it").

| Method & path | Tool | Notes |
|---|---|---|
| `GET /profile` | `read_profile` | The master profile: `{content, version, updated_at}` |
| `PUT /profile` | `save_profile` | Set the profile wholesale (seed / regenerate) |
| `PATCH /profile` | `edit_profile` | Exact-match, in-place edit — `422`/`409` on not-found/not-unique |
| `GET /applications` | `search_applications` | Filters (`status`, `company` fuzzy, `applied_after/before`, `stale`) + free-text `q` (FTS-ranked). No params → newest-first list |
| `POST /applications` | `create_application` | `{company, title, job_description?}` → `{id}` |
| `GET /applications/{id}` | `get_application` | Full row + contacts + `resume_versions` count + `stale` |
| `PATCH /applications/{id}` | `update_application` | Structured fields only (never `final_resume`) |
| `DELETE /applications/{id}` | `delete_application` | Cascade delete; agent gates it behind a confirmation |
| `POST /applications/{id}/contacts` | `add_contact` | `{name, role?, source?, contact_info?, notes?}` |
| `PUT /applications/{id}/resume` | `save_resume` | Replace the tailored resume; snapshots a version |
| `PATCH /applications/{id}/resume` | `edit_resume` | Exact-match edit of the resume; snapshots a version |
| `GET /projects` | `search_projects` | The **evidence library**: filters (`source`, fuzzy `name`) + free-text `q` (FTS) |
| `POST /projects` | `save_project` | Create, or **upsert by `external_id`** (re-reviewing a repo refreshes it, never duplicates) |
| `GET /projects/{id}` | `get_project` | Full project |
| `PATCH /projects/{id}` | `update_project` | Update fields; `409` on an `external_id` collision |
| `DELETE /projects/{id}` | `delete_project` | Remove it (agent gates behind confirmation) |
| `GET /health` | — | No auth: `{status, dossier, database}` |

### Exact-match edits (no silent corruption)

`edit_profile` and `edit_resume` take `{old_string, new_string, replace_all?}` and refuse to
guess: if `old_string` is absent they return **422** ("re-read and copy exact text"); if it
appears more than once and `replace_all` is false they return **409** ("add surrounding
context"). Editing someone's real resume must never silently mangle it — this is the same
discipline openagent-code uses for editing source files.

### Resume versioning & staleness

Every `save_resume` / `edit_resume` records a new immutable row in `resume_versions` and stamps
`profile_version_at_render` with the profile's current `version`. A row reports **`stale: true`**
when the profile has advanced past the version its resume was rendered from — so the coach can
say *"your Stripe resume is a profile-edit behind; want me to refresh it?"*.

---

## Data model

Everything lives in the `careeragent_dossier` schema (never bare `public`), so this service can
later **share one Postgres instance** with the others under its own schema — a config-only switch.

| Table | Holds |
|---|---|
| `profile` | Singleton (`id = 1`) — the master profile `content` + monotonic `version` |
| `applications` | One row per opportunity; a generated `search_vector` (tsvector) powers FTS |
| `contacts` | Points of contact (many per application) |
| `resume_versions` | Immutable history of each application's tailored resume |
| `projects` | The **evidence library** — the user's whole body of work (repos + manual), the source tailored resumes select from per job. Upsert by `external_id` (e.g. GitHub `owner/repo`). See [`specs/0002-projects.md`](specs/0002-projects.md) |

Indexes: **GIN** on `search_vector` (full-text), **GIN `pg_trgm`** on `company` + `title`
(fuzzy), btree on `status` / `last_contact` / `updated_at`.

---

## Configuration

All config is `DOSSIER_*` environment variables, loaded from `.env`. See
[`.env.example`](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `DOSSIER_API_KEY` | — | Inbound `X-API-Key` (the api↔dossier boundary). Required. |
| `DOSSIER_DB_USER` / `_PASSWORD` / `_HOST` / `_PORT` / `_NAME` | `careeragent_dossier` / — / `dossier-db` / `5432` / `careeragent_dossier` | Its own Postgres by default |
| `DOSSIER_DB_SCHEMA` | `careeragent_dossier` | Schema for all objects — keeps the shared-instance switch config-only |
| `DOSSIER_DATABASE_URL` | (unset) | Optional full URL, overrides the parts above |
| `DOSSIER_PORT` | `8006` | Listen port |
| `DOSSIER_ENABLE_DOCS` | `false` | Expose `/docs` + `/openapi.json` |

`DOSSIER_API_KEY` is the **only** boundary key here — dossier calls nothing outbound, so it holds
no other service's secret.

---

## Setup

```bash
# once, shared by all CareerAgent services:
docker network create careeragent-network

cp .env.example .env
# set DOSSIER_API_KEY (python -c "import secrets; print(secrets.token_hex(32))") and DOSSIER_DB_PASSWORD

docker compose up -d --build          # brings up dossier + its Postgres
curl http://localhost:8006/health     # {"status":"ok","dossier":"ok","database":"ok"}
```

`init.sql` runs once on first boot of an empty DB volume: schema, tables, `pg_trgm`, and the
FTS/trigram indexes.

---

## Testing

```bash
# Unit tests (hermetic — no DB, no network): exact-match edit semantics,
# surrogate cleaning, auth, UUID + error mapping.
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests -q

# End-to-end smoke against a running stack (exercises every endpoint):
bash scripts/smoke.sh                                   # Git Bash
powershell -ExecutionPolicy Bypass -File scripts\smoke.ps1   # PowerShell
```

---

## Design decisions

- **Why Postgres, not a vector DB?** Every query here is either a structured filter (status,
  company, date) or a short authoritative document you want *whole*. Neither benefits from
  embeddings; chunking a one-page resume into vectors loses its structure to return fragments.
  Relational + FTS + trigram is simpler, lossless, auditable, and answers the queries a job search
  actually asks.
- **Why FTS + `pg_trgm`?** FTS gives stemming + ranking over free text; trigram gives typo-tolerant
  fuzzy matching on names ("stipe" → "Stripe") and cheap autocomplete. Both live *in* Postgres —
  zero extra infrastructure at one-user scale.
- **Why the profile is one freeform document?** It's the coach's `memory.md`-equivalent — a
  narrative read whole and edited in place. Structure lives in the *applications* table.
- **Why version resumes?** Cheap history + automatic **staleness detection** via
  `profile_version_at_render`.
- **Why no agent logic here?** Keeping dossier a dumb, testable data service means the fence, mode,
  and loop live in exactly one place: the agent in `careeragent-api`.

---

## Known limitations

- **Single-user.** One shared `DOSSIER_API_KEY` and a singleton profile. Multi-user (a `user_id`
  on every row) is a later evolution.
- **No file parsing.** Profile/resume content arrives as *text* from the agent; turning an
  uploaded PDF/DOCX into that text is a separate pipeline (deferred).
- **No vectors / semantic search** — deliberate. If a large corpus of supporting documents ever
  needs semantic retrieval, that would be an additive, separate concern.

---

## License

Apache 2.0 © 2026 William McKeon. See [LICENSE](LICENSE).

---

*careeragent-dossier — part of the CareerAgent system. Port 8006.*
