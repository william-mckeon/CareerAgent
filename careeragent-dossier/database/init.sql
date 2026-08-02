-- =============================================================================
-- careeragent-dossier schema — runs once on first boot of an empty DB volume.
--
-- Everything lives in the `careeragent_dossier` schema (NOT bare public) so the
-- service can later SHARE a single Postgres instance with the other services
-- under its own schema — a config-only switch (point DOSSIER_DB_HOST/NAME at it).
--
-- No vectors. Lookup is structured filters + Postgres full-text search (the
-- generated `search_vector` + GIN index) + trigram fuzzy matching (pg_trgm).
-- See specs/0001-dossier.md.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS careeragent_dossier;
SET search_path TO careeragent_dossier;

-- Trigram fuzzy matching for company/contact names ("stipe" -> "Stripe").
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- -----------------------------------------------------------------------------
-- profile — the master career/project history. Singleton (single-user for now):
-- one row, id = 1. The source of truth every tailored resume is rendered from.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS profile (
    id          integer     PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    content     text        NOT NULL DEFAULT '',
    version     integer     NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Seed the single row so GET /profile always has something to return.
INSERT INTO profile (id, content, version) VALUES (1, '', 0)
    ON CONFLICT (id) DO NOTHING;

-- -----------------------------------------------------------------------------
-- applications — one row per opportunity. final_resume is the tailored resume;
-- search_vector is a STORED generated column (immutable 2-arg to_tsvector) that
-- powers full-text search over the free-text fields.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS applications (
    id                        uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    company                   text        NOT NULL,
    title                     text        NOT NULL,
    job_description           text        NOT NULL DEFAULT '',
    final_resume              text        NOT NULL DEFAULT '',
    status                    text        NOT NULL DEFAULT 'draft',
    applied_at                timestamptz,
    last_contact              timestamptz,
    next_follow_up            date,
    posting_url               text,
    location                  text,
    salary_range              text,
    notes                     text,
    profile_version_at_render integer,
    search_vector             tsvector GENERATED ALWAYS AS (
        to_tsvector(
            'english',
            coalesce(company, '')         || ' ' ||
            coalesce(title, '')           || ' ' ||
            coalesce(job_description, '') || ' ' ||
            coalesce(final_resume, '')    || ' ' ||
            coalesce(notes, '')
        )
    ) STORED,
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS applications_search       ON applications USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS applications_company_trgm ON applications USING GIN (company gin_trgm_ops);
CREATE INDEX IF NOT EXISTS applications_title_trgm   ON applications USING GIN (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS applications_status       ON applications (status);
CREATE INDEX IF NOT EXISTS applications_last_contact ON applications (last_contact);
CREATE INDEX IF NOT EXISTS applications_by_updated   ON applications (updated_at DESC);

-- -----------------------------------------------------------------------------
-- contacts — points of contact on an application (many per application).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contacts (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id  uuid        NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    name            text        NOT NULL,
    role            text,                   -- hiring manager | recruiter | referral | ...
    source          text,                   -- LinkedIn | email | referral | ...
    contact_info    text,
    notes           text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS contacts_by_application ON contacts (application_id);

-- -----------------------------------------------------------------------------
-- preferences — agent-authored durable coaching preferences (P7 #17). User-STATED
-- standing instructions the coach pins each turn ("targets senior PM", "metric-
-- first bullets", "one page"). NOT career evidence: deliberately kept OUT of the
-- grounding corpus so a preference can never back an invented resume claim
-- (ADR-002). Injected whole, never full-text searched -> no search_vector.
-- (Kept in sync with database/migrations/0004_preferences.sql.)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS preferences (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    content     text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS preferences_by_created ON preferences (created_at);

-- -----------------------------------------------------------------------------
-- resume_versions — immutable history of an application's tailored resume; one
-- row per save/edit, monotonic version per application. Never overwritten.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS resume_versions (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id  uuid        NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    version         integer     NOT NULL,
    content         text        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (application_id, version)
);
CREATE INDEX IF NOT EXISTS resume_versions_by_application ON resume_versions (application_id, version DESC);

-- -----------------------------------------------------------------------------
-- resume_artifacts — BINARY rendered résumé documents (PDF/DOCX bytes) produced by
-- careeragent-render (P7 #16). Stored here so the bytes never ride the coach's tool
-- result or the /chat SSE content stream; served on demand via
-- GET /applications/{id}/artifact. One monotonic version per application. The
-- ats_* columns are reserved for pairing an artifact with its coverage score.
-- (Kept in sync with database/migrations/0005_resume_artifacts.sql.)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS resume_artifacts (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id  uuid        NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    version         integer     NOT NULL,
    format          text        NOT NULL,
    filename        text        NOT NULL,
    content         bytea       NOT NULL,
    byte_size       integer     NOT NULL,
    ats_score       integer,
    ats_coverage    text,
    ats_matched     jsonb,
    ats_missing     jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (application_id, version)
);
CREATE INDEX IF NOT EXISTS resume_artifacts_by_application ON resume_artifacts (application_id, version DESC);

-- -----------------------------------------------------------------------------
-- projects — the user's full body of work (the "whole picture"): repos they own
-- or contributed to, plus manually-added projects. The evidence library the
-- master profile is distilled from and tailored resumes SELECT from per job.
-- (Kept in sync with database/migrations/0002_projects.sql.)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name             text        NOT NULL,
    source           text        NOT NULL DEFAULT 'manual',  -- github | manual | resume
    external_id      text,                                   -- e.g. GitHub 'owner/repo' for upsert/dedupe
    repo_url         text,
    summary          text        NOT NULL DEFAULT '',
    role             text,
    tech_stack       text,                                   -- freeform, e.g. "Python, FastAPI, Postgres"
    highlights       text,                                   -- markdown bullets: key accomplishments + evidence
    languages        text,                                   -- from GitHub, e.g. "Python 82%, C++ 18%"
    stars            integer,
    last_reviewed_at timestamptz,
    commit_sha       text,                                   -- reviewed HEAD sha; idempotency: skip re-review if unchanged
    search_vector    tsvector GENERATED ALWAYS AS (
        to_tsvector(
            'english',
            coalesce(name, '')       || ' ' ||
            coalesce(summary, '')    || ' ' ||
            coalesce(role, '')       || ' ' ||
            coalesce(tech_stack, '') || ' ' ||
            coalesce(highlights, '') || ' ' ||
            coalesce(languages, '')
        )
    ) STORED,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS projects_external_id
    ON projects (external_id) WHERE external_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS projects_search     ON projects USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS projects_name_trgm  ON projects USING GIN (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS projects_source     ON projects (source);
CREATE INDEX IF NOT EXISTS projects_by_updated ON projects (updated_at DESC);
