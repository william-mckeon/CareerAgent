-- =============================================================================
-- careeragent-dossier migration 0002 — the projects evidence library.
--
-- IDEMPOTENT. Safe to run against a LIVE database (it only CREATEs IF NOT
-- EXISTS), so it adds the projects tier WITHOUT wiping the existing volume —
-- your saved master profile and applications are preserved. `init.sql` carries
-- the same DDL for fresh installs.
--
-- Apply:
--   docker exec -i careeragent-dossier-db psql -U careeragent_dossier \
--     -d careeragent_dossier < database/migrations/0002_projects.sql
-- =============================================================================

SET search_path TO careeragent_dossier;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- projects — the user's full body of work (the "whole picture"): repos they own
-- or contributed to, plus manually-added projects. The evidence library the
-- master profile is distilled from and tailored resumes SELECT from per job.
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

-- Upsert key: one row per GitHub repo (owner/repo). Partial so manually-added
-- projects (external_id NULL) are never constrained.
CREATE UNIQUE INDEX IF NOT EXISTS projects_external_id
    ON projects (external_id) WHERE external_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS projects_search     ON projects USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS projects_name_trgm  ON projects USING GIN (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS projects_source     ON projects (source);
CREATE INDEX IF NOT EXISTS projects_by_updated ON projects (updated_at DESC);
