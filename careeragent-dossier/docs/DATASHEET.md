# careeragent-dossier — Datasheet

> Contract reference for engineers integrating with or operating careeragent-dossier.
> The README is the narrative introduction; this file is the precise contract.

---

## Quick Reference

| Item | Value |
|---|---|
| Role | Career data system-of-record (master profile + application tracker) |
| Kind | Async FastAPI HTTP service + its own Postgres |
| Language / base image | Python 3.12 · `python:3.12-slim` |
| Host / container port | `8006` / `8006` |
| Sole client | `careeragent-api` (the agent) |
| Auth in | `X-API-Key: DOSSIER_API_KEY` on every endpoint except `/health` |
| Auth out | None (calls nothing outbound) |
| Store | Postgres 16, schema `careeragent_dossier` |
| Lookup | Structured filters + full-text search (tsvector/GIN) + trigram fuzzy (`pg_trgm`) |
| Vectors / embeddings | **None** (deliberate) |
| Version | 0.1.0 |

---

## Ownership boundaries

### What this service owns

| Domain | Concrete artifact |
|---|---|
| The master profile (singleton document + version) | `profile` table |
| The application tracker | `applications` table |
| Points of contact (many per application) | `contacts` table |
| Tailored-resume version history | `resume_versions` table |
| The projects evidence library | `projects` table |
| Structured + full-text + fuzzy lookup | `store.search_applications` / `search_projects` (FTS + `pg_trgm`) |
| Exact-match edit safety | `store._apply_edit` (`edit_profile` / `edit_resume`) |
| Inbound auth | `security.verify_api_key` (`DOSSIER_API_KEY`) |

### What this service does NOT own

| Concern | Owner |
|---|---|
| The tool-calling loop, tool selection | `careeragent-api` (the agent) |
| Permission fence / mode (plan / acceptEdits) | `careeragent-api` |
| The resume-coach persona | `careeragent-api` |
| Model / inference | `careeragent-infra` → Bedrock |
| Conversation transcript | `careeragent-sessions` |
| File (PDF/DOCX) parsing to text | Not implemented (deferred pipeline) |
| Semantic / vector search | Not implemented (deliberate) |

Dossier never decides *whether* an edit is allowed — only performs it. The judgment lives in the
agent.

---

## API reference

All endpoints require `X-API-Key: DOSSIER_API_KEY` except `GET /health`. Bodies are JSON;
responses are JSON (datetimes as ISO-8601, ids as UUID strings).

### Profile

| Endpoint | Body | Returns |
|---|---|---|
| `GET /profile` | — | `{content, version, updated_at}` |
| `PUT /profile` | `{content}` | `{content, version}` (version bumped) |
| `PATCH /profile` | `{old_string, new_string, replace_all?}` | `{content, version}` · `422` not-found · `409` not-unique |

### Applications

| Endpoint | Body / query | Returns |
|---|---|---|
| `GET /applications` | query: `status`, `company`, `q`, `applied_after`, `applied_before`, `stale`, `limit≤200`, `offset` | `[{id, company, title, status, last_contact, updated_at, stale, rank}]` (newest-first; FTS-ranked when `q`) |
| `POST /applications` | `{company, title, job_description?}` | `201 {id}` |
| `GET /applications/{id}` | — | full row + `contacts[]` + `resume_versions` (count) + `stale` · `404` |
| `PATCH /applications/{id}` | any of `status, last_contact, next_follow_up, applied_at, posting_url, location, salary_range, notes, company, title, job_description` | updated row · `404` |
| `DELETE /applications/{id}` | — | `{deleted}` · `404` |

`last_contact` / `applied_at` accept ISO-8601 datetimes (e.g. `2026-06-30T12:00:00Z`);
`next_follow_up` accepts a date (`2026-07-05`). They are parsed to native types before binding.

### Contacts & resume

| Endpoint | Body | Returns |
|---|---|---|
| `POST /applications/{id}/contacts` | `{name, role?, source?, contact_info?, notes?}` | `201 {contact_id}` · `404` |
| `PUT /applications/{id}/resume` | `{content}` | `{version}` (snapshots a version, stamps `profile_version_at_render`) · `404` |
| `PATCH /applications/{id}/resume` | `{old_string, new_string, replace_all?}` | `{content, version}` · `422`/`409`/`404` |

### Projects (the evidence library)

| Endpoint | Body / query | Returns |
|---|---|---|
| `GET /projects` | query: `q`, `source`, `name` (fuzzy), `limit≤200`, `offset` | `[{id, name, source, repo_url, tech_stack, updated_at, rank}]` (newest-first; FTS-ranked when `q`) |
| `POST /projects` | `{name, summary?, repo_url?, source?, external_id?, role?, tech_stack?, highlights?, languages?, stars?}` | `201 {id, upserted}` — upsert by `external_id`; only fields actually sent are written · `409` conflict |
| `GET /projects/{id}` | — | full row · `404` |
| `PATCH /projects/{id}` | any project field | updated row · `404` · `409` `external_id` conflict |
| `DELETE /projects/{id}` | — | `{deleted}` · `404` |

### Health

`GET /health` (no auth) → `{"status":"ok"|"degraded","dossier":"ok","database":"ok"|"unreachable"}`.

### Error shape

`{"detail": "<message>"}`. Codes: `400` bad UUID / missing required field · `401` bad/missing
key · `404` unknown application/project · `409` edit not-unique **or** duplicate `external_id` ·
`422` edit not-found · `500` unexpected.

---

## State model

Schema `careeragent_dossier` (created by `database/init.sql` on first boot):

```
profile(id=1 singleton, content text, version int, created_at, updated_at)
applications(id uuid, company, title, job_description, final_resume, status,
             applied_at, last_contact, next_follow_up, posting_url, location,
             salary_range, notes, profile_version_at_render,
             search_vector tsvector GENERATED, created_at, updated_at)
contacts(id uuid, application_id → applications ON DELETE CASCADE,
         name, role, source, contact_info, notes, created_at)
resume_versions(id uuid, application_id → applications ON DELETE CASCADE,
                version int, content, created_at, UNIQUE(application_id, version))
projects(id uuid, name, source, external_id, repo_url, summary, role, tech_stack,
         highlights, languages, stars, last_reviewed_at,
         search_vector tsvector GENERATED, created_at, updated_at;
         partial UNIQUE(external_id) WHERE external_id IS NOT NULL)   -- the upsert key
```

- `search_vector` is a **STORED generated column** over company/title/JD/resume/notes (immutable
  2-arg `to_tsvector`), indexed with GIN.
- `stale` is **derived at query time**: `profile_version_at_render IS NOT NULL AND
  profile.version > profile_version_at_render`.
- Persistence is a Docker volume (`dossier-db-data`). No cross-request in-process state.

---

## Configuration

| Variable | Default | Contractual? |
|---|---|---|
| `DOSSIER_API_KEY` | — | Yes — inbound auth |
| `DOSSIER_DB_USER` / `_PASSWORD` / `_HOST` / `_PORT` / `_NAME` | `careeragent_dossier` / — / `dossier-db` / `5432` / `careeragent_dossier` | Yes — DB connection |
| `DOSSIER_DB_SCHEMA` | `careeragent_dossier` | Yes — objects live here; enables shared-instance switch |
| `DOSSIER_DATABASE_URL` | (unset) | Optional — full URL overrides the parts |
| `DOSSIER_PORT` | `8006` | Reference |
| `DOSSIER_ENABLE_DOCS` | `false` | Reference — exposes `/docs` |

---

## Container / deployment

- **Image:** `careeragent-dossier:0.1.0`, non-root (`appuser`), stdlib `/health` HEALTHCHECK.
- **Compose:** two services — `careeragent-dossier` + `careeragent-dossier-db` (postgres:16) — on
  the external `careeragent-network`. `init.sql` is mounted into the DB's init dir.
- **Volumes:** `dossier-db-data` (the Postgres data). The app container is stateless.
- **Restart policy:** `unless-stopped`.
- **Shared-DB path:** point `DOSSIER_DB_HOST`/`_NAME` at a shared instance, keep
  `DOSSIER_DB_SCHEMA=careeragent_dossier` — no code change.

---

## Failure modes

| Condition | Behaviour |
|---|---|
| DB unreachable | `/health` → `{"status":"degraded","database":"unreachable"}`; endpoints error `500` |
| Edit `old_string` not found | `422` with a teaching message; **no write** |
| Edit `old_string` not unique | `409` with a teaching message; **no write** |
| Unknown application id | `404` |
| Malformed UUID in path | `400` |
| Missing/invalid `X-API-Key` | `401` (except `/health`) |
| Lone UTF-16 surrogate in stored text | Stripped on write (`_clean`) — never a Postgres/JSON encode failure |

---

## Cross-references

- `specs/0001-dossier.md` — the contract & design rationale (source of truth)
- `src/backend/api.py` — endpoints (source of truth for the API reference)
- `src/store.py` — persistence, FTS/trigram queries, exact-match edits
- `database/init.sql` — schema + indexes (source of truth for the state model)
- `README.md` — narrative introduction
- `scripts/smoke.sh` / `scripts/smoke.ps1` — end-to-end verification

---

*careeragent-dossier — part of the CareerAgent system. Port 8006.*
