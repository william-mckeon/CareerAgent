# 0002 — careeragent-dossier: the projects evidence library

> Adds a **projects** tier to careeragent-dossier: the user's *whole body of work* — every
> significant project they built or contributed to, richer than the one-page resume. It is the
> evidence base the master profile is distilled from and that tailored resumes **select from per
> job**. Same store, same house pattern as applications; no new service. Extends
> [`0001-dossier.md`](0001-dossier.md).

---

## Goal

A resume is one page; a career is not. The master profile is a curated narrative, and a tailored
resume is a one-page projection of it — both are *lossy*. Different jobs need different slices of the
same work: a platform role surfaces orchestration projects, an ML role surfaces RAG/eval projects.
To tailor well the coach needs the **full evidence base** to *select from*, not just the distillation.

`projects` is that base: a structured, queryable record of each project (name, repo, tech, the user's
role, key accomplishments + evidence), so surfacing the right projects for a job becomes a **query**,
not a rewrite. GitHub review (a later phase) populates it; the coach can also add projects from
conversation today.

## Concepts

- **project** — one thing the user built or contributed to: `name`, `summary`, optional `repo_url`,
  `role`, `tech_stack`, `highlights` (markdown bullets of accomplishments + evidence), `languages`,
  `stars`, `source` (`github` | `manual` | `resume`), `last_reviewed_at`.
- **external_id** — a stable identity for de-dupe/upsert, e.g. a GitHub `owner/repo`. Re-reviewing a
  repo **updates** its row instead of duplicating. Manually-added projects leave it null.
- **evidence library** — the whole set of projects. The master profile is the *distillation*;
  applications' resumes are *projections*; the library is the *source evidence*.

## Where it sits

Unchanged from 0001: dossier is a leaf; `careeragent-api` (the agent) is its only client. The agent
gets five new tools (`search_projects`, `get_project`, `save_project`, `update_project`,
`delete_project`) that map 1:1 to the endpoints below.

## Contract (HTTP)  — all `X-API-Key`

**`GET /projects`** → `search_projects`
Query params (all optional, ANDed): `q` (free-text FTS over name/summary/role/tech/highlights/
languages), `source`, `name` (fuzzy, trigram), `limit` (≤200), `offset`. No params → newest-first.
Returns summaries: `[{id, name, source, repo_url, tech_stack, updated_at, rank}]`.

**`POST /projects`** → `save_project` (create **or upsert**)
Body: `{name (required), summary?, repo_url?, source?, external_id?, role?, tech_stack?, highlights?,
languages?, stars?}`. If `external_id` is supplied and already exists → the row is **updated in
place**; else inserted. Returns `{id, upserted}`.

**`GET /projects/{id}`** → `get_project` — full row (minus the internal FTS column). `404` if unknown.

**`PATCH /projects/{id}`** → `update_project` — update any subset of the fields; all-None is a no-op.
`404` if unknown.

**`DELETE /projects/{id}`** → `delete_project` — remove it. `404` if unknown. (The agent gates this
behind confirmation via the permission engine.)

## Store (schema `careeragent_dossier`)

```
projects(
  id               uuid pk default gen_random_uuid(),
  name             text not null,
  source           text not null default 'manual',   -- github | manual | resume
  external_id      text,                              -- e.g. GitHub 'owner/repo'
  repo_url         text,
  summary          text not null default '',
  role             text,
  tech_stack       text,
  highlights       text,
  languages        text,
  stars            integer,
  last_reviewed_at timestamptz,
  search_vector    tsvector GENERATED (name+summary+role+tech_stack+highlights+languages) STORED,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
)
```
Indexes: **partial UNIQUE** on `external_id WHERE external_id IS NOT NULL` (the upsert key), **GIN**
on `search_vector` (FTS), **GIN `pg_trgm`** on `name` (fuzzy), btree on `source` / `updated_at`.

**Migration:** the table ships in `init.sql` (fresh installs) **and** in
`database/migrations/0002_projects.sql` — idempotent (`CREATE … IF NOT EXISTS`), so it applies to the
**live** DB without a volume wipe, preserving the saved profile + applications.

## Behavior rules

1. **Upsert by external_id** — `save_project` with an `external_id` that exists updates that row
   (`ON CONFLICT (external_id) WHERE external_id IS NOT NULL DO UPDATE`), so re-reviewing a repo never
   duplicates. Without `external_id`, it always inserts. `upserted` in the response reports which.
2. **Column allowlist** — create/update only touch the fixed `_PROJECT_FIELDS` set; the sole place a
   projects column name is interpolated into SQL, so it's injection-safe (same discipline as
   `update_application`).
3. **Lookup** — `q` ranks by FTS `ts_rank`; `name` fuzzy-matches by trigram; filters AND. No vectors.
4. **`source` is soft** — a documented set, not DB-enforced, so an unexpected value never blocks a write.

## Acceptance

- [ ] `POST /projects {name}` creates a row (`source="manual"`, `upserted=false`); `GET /projects/{id}`
      returns it; unknown id → `404`.
- [ ] `POST /projects` twice with the same `external_id` yields **one** row, second call
      `upserted=true`, fields refreshed.
- [ ] `GET /projects?q=…` full-text-ranks; `?name=…` fuzzy-matches (`stipe`→`Stripe`-style);
      `?source=github` filters; no params → newest-first.
- [ ] `PATCH /projects/{id}` updates supplied fields only; `DELETE` removes; both `404` on unknown.
- [ ] Missing/invalid `X-API-Key` → `401`; the migration applies to a **live** DB with the existing
      profile + applications intact.
- [ ] The other services (api aside from its new tools, infra, sessions, memory, logger, frontend)
      are unchanged.

## Non-goals (this spec)

- **GitHub review / MCP** — populating projects *from* GitHub is a later phase (MCP client + the
  official server + subagent repo-review). This spec is just the store + tools; the coach can add
  projects from conversation now.
- **Projects → resume auto-selection** — the agent decides which projects to feature when tailoring
  (prompt-driven); no server-side ranking beyond FTS/trigram lookup.
- **Multi-user** — single shared key, like the rest of dossier.

## Design decisions

- **Why a separate tier, not just the profile?** The profile is a one-page narrative; the library is
  *many* structured projects. Per-job selection ("which projects prove distributed-systems chops")
  is a **query** against structure, which prose can't answer. Same reason applications are rows, not
  a document.
- **Why upsert by `external_id`?** GitHub is the intended source and repos get re-reviewed; keying on
  `owner/repo` makes re-review idempotent (refresh, not duplicate) while leaving manual projects
  unconstrained (partial unique index).
- **Why freeform `tech_stack` / `highlights` (text, not normalized tables)?** At one-user scale, FTS
  over freeform text is simpler and more than enough for lookup; normalizing skills into join tables
  is complexity the tailoring flow doesn't need. Revisit only if cross-project skill analytics appear.

---

*careeragent-dossier projects — part of the CareerAgent system. Port 8006.*
