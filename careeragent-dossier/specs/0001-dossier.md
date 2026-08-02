# 0001 — careeragent-dossier

> The **career data system-of-record** for CareerAgent. It owns the user's **master profile**
> (the durable career/project history) and the **application tracker** (one row per job
> application — company, role, JD, the tailored resume, contacts, status, timeline). It is a
> plain relational store — **no vectors, no embeddings** — exposing a small set of tool
> endpoints. Its **only client is `careeragent-api`** (the agent). It is a new service and
> changes **no existing service's code**.

---

## Goal

The resume coach needs somewhere durable, structured, and queryable to keep the material it
works from and produces: your career history, and every application you're running. A resume is
short, structured, and authoritative — you always want it *whole*, and you query the tracker by
real fields ("still interviewing", "no contact in two weeks", "applications to fintech"). That is
relational work, not semantic search.

`careeragent-dossier` provides exactly that and nothing more: it **stores the master profile**,
**stores the applications** (with their tailored resumes, contacts, and timeline), and offers a
**structured + full-text + fuzzy lookup** over them — all as HTTP endpoints the agent calls as
tools. It holds **no agent logic**: no loop, no permission engine, no persona. Those live in
`careeragent-api`. Dossier is the nouns; the agent is the verbs.

## Concepts

- **master profile** — a single, living document (freeform markdown) of your raw career/project
  history: roles, projects, impact, skills. The **source of truth** every tailored resume is
  rendered *from*. Read whole, edited in place. Carries a monotonic `version`.
- **application** — one row per opportunity: `company`, `title`, `job_description`, the
  `final_resume` tailored for it, `status`, timeline (`applied_at`, `last_contact`,
  `next_follow_up`), and loose fields (`posting_url`, `location`, `salary_range`, `notes`).
- **contact** — a point of contact on an application (hiring manager / recruiter / referral),
  with `name`, `role`, `source`, optional `contact_info`. An application may have **several**.
- **resume version** — an immutable snapshot of an application's `final_resume`, captured on
  every `save`/`edit`, so a row's resume history is never lost.
- **lookup** — structured filters (status/company/date) + Postgres **full-text search** (FTS,
  stemmed + ranked) over free text + **trigram** (`pg_trgm`) fuzzy matching for names. No vectors.

## Where it sits

```
careeragent-api (:8001, the agent) ── HTTP tools ──> careeragent-dossier (:8006) ──> Postgres
```

Dossier is a **leaf**: it calls nothing outbound. It is not on the chat path and is never reached
by the frontend. The agent (`api`) is its sole client, exactly as `memory` is a client of `api`.

## Contract (HTTP)

Each endpoint is one agent tool (see [`careeragent-api/specs/0001-agent.md`]). All require
`X-API-Key` except `/health`. **Read** endpoints are the ones allowed in the agent's read-only
`plan` mode; **write** endpoints are gated to `acceptEdits`.

### Read

**`GET /profile`** → `read_profile`
Return the master profile: `{content, version, updated_at}`. Always exists (seeded empty).

**`GET /applications`** → `search_applications`
Query params, all optional and **ANDed**: `status`, `company` (fuzzy, trigram), `q` (free-text
over company/title/JD/resume/notes, FTS-ranked), `applied_after`, `applied_before`,
`stale` (bool — resume older than current profile version), `limit` (default 50, max 200),
`offset`. No params → list all, newest first. Returns ranked summaries:
`[{id, company, title, status, last_contact, updated_at, stale, rank}]`.

**`GET /applications/{id}`** → `get_application`
Full row + its contacts + resume version count:
`{id, company, title, job_description, final_resume, status, applied_at, last_contact,
next_follow_up, posting_url, location, salary_range, notes, profile_version_at_render, stale,
contacts:[{id, name, role, source, contact_info, notes}], resume_versions: <int>, created_at,
updated_at}`. `404` if unknown.

### Write

**`PUT /profile`** → `save_profile`
Set the master profile wholesale — seed it (from an interview or an uploaded
resume) or replace it outright: `{content}`. Bumps `version`, returns
`{content, version}`. `edit_profile` handles precise tweaks afterward; this is
the clean "first write / regenerate" path (exact-match can't seed an empty doc).

**`PATCH /profile`** → `edit_profile`
Exact-match, in-place edit: `{old_string, new_string, replace_all?}`. **No silent corruption** —
`old_string` absent → `422` ("not found; re-read the profile and copy exact text");
found >1 and not `replace_all` → `409` ("not unique; add surrounding context"). On success
bumps `version`, returns `{content, version}`.

**`POST /applications`** → `create_application`
`{company, title, job_description?}` → creates a row (`status` defaults to `"draft"`), returns
`{id}`.

**`PATCH /applications/{id}`** → `update_application`
Update any subset of the **structured** fields (`status`, `last_contact`, `next_follow_up`,
`applied_at`, `posting_url`, `location`, `salary_range`, `notes`, `company`, `title`,
`job_description`). Never touches `final_resume` (that has its own endpoints). Returns the
updated row. `404` if unknown.

**`DELETE /applications/{id}`** → `delete_application`
Remove an application and its contacts + resume history (cascade). Destructive, so the agent
gates it behind an explicit confirmation (an `ask` in the permission engine). Returns
`{deleted}`; `404` if unknown.

**`POST /applications/{id}/contacts`** → `add_contact`
`{name, role?, source?, contact_info?, notes?}` → `{contact_id}`. `404` if the application is
unknown.

**`PUT /applications/{id}/resume`** → `save_resume`
Replace the tailored resume wholesale: `{content}`. Snapshots the prior resume into
`resume_versions`, stamps `profile_version_at_render` = current profile `version`, returns
`{version}` (the new snapshot number). Used after the agent drafts a fresh resume from the
profile + JD.

**`PATCH /applications/{id}/resume`** → `edit_resume`
Exact-match, in-place edit of `final_resume`: `{old_string, new_string, replace_all?}`. Same
no-silent-corruption rules as `edit_profile` (`422`/`409`). Snapshots the prior resume, returns
`{content, version}`.

### `GET /health` — no auth
House shape: `{status: "ok"|"degraded", dossier: "ok", database: "ok"|"unreachable"}`.

## Store (own Postgres, schema `careeragent_dossier`, extension `pg_trgm`)

```
profile(                              -- singleton (single-user for now)
  id           integer primary key default 1 check (id = 1),
  content      text        not null default '',
  version      integer     not null default 0,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
)
applications(
  id                         uuid primary key default gen_random_uuid(),
  company                    text        not null,
  title                      text        not null,
  job_description            text        not null default '',
  final_resume               text        not null default '',
  status                     text        not null default 'draft',   -- draft|applied|interviewing|offer|rejected|ghosted (soft)
  applied_at                 timestamptz,
  last_contact               timestamptz,
  next_follow_up             date,
  posting_url                text,
  location                   text,
  salary_range               text,
  notes                      text,
  profile_version_at_render  integer,                                -- profile.version when final_resume was last written
  search_vector              tsvector,                               -- maintained over company/title/JD/resume/notes
  created_at                 timestamptz not null default now(),
  updated_at                 timestamptz not null default now()
)
contacts(
  id              uuid primary key default gen_random_uuid(),
  application_id  uuid not null references applications(id) on delete cascade,
  name            text not null,
  role            text,                                              -- hiring manager | recruiter | referral | ...
  source          text,                                              -- LinkedIn | email | referral | ...
  contact_info    text,
  notes           text,
  created_at      timestamptz not null default now()
)
resume_versions(
  id              uuid primary key default gen_random_uuid(),
  application_id  uuid not null references applications(id) on delete cascade,
  version         integer not null,                                  -- monotonic per application
  content         text not null,
  created_at      timestamptz not null default now(),
  unique(application_id, version)
)
```
Indexes: **GIN** on `applications.search_vector` (FTS); **GIN `pg_trgm`** on `company` and
`title` (fuzzy); btree on `status`, `last_contact`. `stale` is derived at query time:
`profile.version > applications.profile_version_at_render`.

**Every object is created in the `careeragent_dossier` schema** (schema-qualified, never bare
`public`) so the service can later share one Postgres instance with no code change — same pattern
as `careeragent-sessions`.

## Auth (compartmentalized, house pattern)

- **Inbound** `X-API-Key` = `DOSSIER_API_KEY` (api↔dossier). Constant-time compare; `/health`
  is unauthenticated.
- **Outbound**: none. Dossier calls no other service, so it holds no other boundary key.

## Precedence / behavior rules

1. **Exact-match edits** (`edit_profile`, `edit_resume`): 0 matches → `422` with a teaching
   message; >1 and not `replace_all` → `409` with a teaching message; else replace. Never a
   partial/ambiguous write.
2. **Resume writes snapshot first**: `save_resume` and `edit_resume` copy the *prior* resume into
   `resume_versions` before writing, then stamp `profile_version_at_render` = current profile
   `version`. History is append-only and never overwritten.
3. **`search_applications`** ANDs all supplied filters; `q` ranks by FTS `ts_rank`; `company`
   fuzzy-matches by trigram similarity; no params → newest-first list. Results are summaries, not
   full rows.
4. `update_application` is for structured fields only; it never edits `final_resume`.
5. `status` is a **soft** enum (documented set, not DB-enforced) so the agent isn't blocked by an
   unforeseen value; the app layer validates against the known set with a warning.
6. Dossier performs **no orchestration** — no permission checks, no loop, no persona. It stores,
   edits (exact-match), and queries. The fence/mode/loop are the agent's job.

## Acceptance

- [ ] `GET /profile` returns the (seeded-empty) profile with `version` 0; `PATCH /profile`
      exact-match edit bumps `version` and returns the new content.
- [ ] `edit_profile` / `edit_resume` return `422` on a not-found `old_string` and `409` on a
      non-unique one (unless `replace_all`), and never write on failure.
- [ ] `POST /applications` creates a row (`status="draft"`); `GET /applications/{id}` returns it
      with an empty `contacts` list; unknown id → `404`.
- [ ] `PUT`/`PATCH .../resume` write the resume, snapshot the prior version into
      `resume_versions`, and stamp `profile_version_at_render`; a later `edit_profile` makes the
      row report `stale: true`.
- [ ] `POST .../contacts` adds a contact; `GET /applications/{id}` shows it.
- [ ] `GET /applications` filters by `status`, fuzzy-matches `company` (e.g. `q=stipe` finds
      "Stripe"), full-text-ranks `q`, and returns newest-first with no params.
- [ ] Missing/invalid `X-API-Key` → `401`; `GET /health` (no auth) → `200` with the documented shape.
- [ ] `init.sql` creates everything in schema `careeragent_dossier` with `pg_trgm` enabled;
      repointing `DOSSIER_DB_*` at a shared instance requires **no code change**.
- [ ] The existing services (infra, api, frontend, memory, logger, sessions) are unchanged.

## Non-goals (this spec)

- **Vectors / embeddings / semantic search** — explicitly out. The whole point: structured +
  FTS + trigram cover every lookup this needs. A VDB would be the wrong tool for a short
  authoritative document you always read whole.
- **File upload / parsing (Path A ingest)** — profile and resume content arrive as **text** from
  the agent. Turning an uploaded PDF/DOCX into that text is a later pipeline; it does not change
  this store's shape.
- **GitHub / external sourcing** — the profile is filled by the agent (interview) for now; GitHub
  is a later profile-bootstrap source.
- **Multi-user / per-user ownership** — a single shared `DOSSIER_API_KEY` and a singleton
  profile for now, matching the rest of the stack. Per-user identity (a `user_id` on every row)
  is a later evolution.
- **Agent behavior** — the loop, tool-selection, permission fence/mode, and persona all live in
  `careeragent-api`. Dossier never decides *whether* an edit is allowed, only performs it.
- **Shared-instance DB as default** — supported by config; the default is a separate Postgres.

## Design Decisions

- **Why Postgres, not a vector DB?** Everything here is either a structured query (status, company,
  date) or a short authoritative document you want *whole*. Neither benefits from embeddings, and
  chunking a one-page resume into vectors loses its structure to retrieve fragments. Relational +
  FTS + trigram is simpler, lossless, auditable, and answers the queries a job search actually asks.
- **Why FTS + `pg_trgm` for lookup?** FTS gives stemming ("manage" matches "manager") and ranking
  over the free-text fields; trigram gives typo-tolerant fuzzy matching on names ("stipe" → "Stripe")
  and cheap autocomplete. Both live *in* Postgres — zero extra infrastructure, no separate search
  service to feed and sync at one-user scale.
- **Why the profile is one freeform document, not structured rows?** It's the coach's
  `memory.md`-equivalent — a narrative the agent reads whole and edits in place. Structure lives in
  the *applications* table (the outputs); the *source* is prose, which is how careers actually read.
- **Why snapshot resume versions?** Cheap (a few KB of text) and it buys two things: never losing a
  prior tailored resume, and **staleness detection** — `profile_version_at_render` vs the current
  profile `version` tells the agent "your Stripe resume is a profile-edit behind; want to refresh?".
- **Why exact-match edits (borrowed from openagent-code)?** Editing someone's real resume must never
  silently corrupt it. Exact-match-or-fail forces every change to be grounded in text that was
  actually read and fails loudly (with a teaching message) instead of guessing.
- **Why does dossier hold no agent logic?** Keeping it a dumb, fully-testable data service — CRUD +
  lookup, no loop, no permissions — means the risky, judgment-heavy parts (the fence, the mode, the
  loop) live in exactly one place: the agent in `api`. Clean noun/verb split.
- **Why own DB now, shared later?** Separate is simpler and isolated for dev; schema-qualifying to
  `careeragent_dossier` + env-driven connection keeps "share one Postgres instance" a config-only
  switch, exactly like `careeragent-sessions`.

---

*careeragent-dossier — part of the CareerAgent system. Port 8006.*
