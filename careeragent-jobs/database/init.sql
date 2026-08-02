-- =============================================================================
-- careeragent-jobs schema — runs once on first boot of an empty DB volume.
--
-- Everything lives in the `careeragent_jobs` schema (NOT bare public) so the
-- service can later share a single Postgres instance with its siblings under its
-- own schema — a config-only switch (point JOBS_DB_HOST/NAME at it, keep
-- JOBS_DB_SCHEMA=careeragent_jobs). The same table is also created idempotently
-- at service startup (store.ensure_schema) so an existing DB volume gets it
-- without a re-init. See database/migrations/0001_jobs.sql and specs/0001-jobs.md.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS careeragent_jobs;
SET search_path TO careeragent_jobs;

-- gen_random_uuid() is built into Postgres core (>=13), matching the siblings'
-- reliance on it. pgcrypto is not required on Postgres 16.

-- A background job: a kind + an opaque spec, run off the request path by the
-- worker, whose result is injected into `conversation_id` on completion.
CREATE TABLE IF NOT EXISTS jobs (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            text        NOT NULL,
    spec            jsonb       NOT NULL DEFAULT '{}'::jsonb,
    conversation_id uuid,
    status          text        NOT NULL DEFAULT 'pending',   -- pending|running|done|failed
    attempts        integer     NOT NULL DEFAULT 0,
    result          text,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Partial index over the claim path: the worker's claim_one() scans only pending
-- rows, oldest first.
CREATE INDEX IF NOT EXISTS jobs_claimable
    ON jobs (created_at) WHERE status = 'pending';

-- Fast "jobs for this conversation, newest first" lookups (GET /jobs).
CREATE INDEX IF NOT EXISTS jobs_by_conversation
    ON jobs (conversation_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- P7 #18b — recurring schedules + a tiny key/value settings table. See
-- database/migrations/0002_schedules.sql for the rationale. The scheduler loop
-- (src/scheduler.py) enqueues a job per due schedule; jobs_settings holds the
-- singleton "🔔 Reminders" conversation id scheduled results are injected into.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schedules (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name             text        UNIQUE NOT NULL,
    kind             text        NOT NULL,
    spec             jsonb       NOT NULL DEFAULT '{}'::jsonb,
    interval_seconds integer     NOT NULL,
    next_run         timestamptz NOT NULL DEFAULT now(),
    enabled          boolean     NOT NULL DEFAULT true,
    last_run         timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS schedules_due
    ON schedules (next_run) WHERE enabled;

CREATE TABLE IF NOT EXISTS jobs_settings (
    key   text PRIMARY KEY,
    value text
);
