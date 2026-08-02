-- =============================================================================
-- 0002_schedules.sql — recurring schedules + a tiny key/value settings table.
--
-- P7 #18b (cron / recurring reminders). A scheduler loop (src/scheduler.py) reads
-- `schedules` whose next_run has arrived and ENQUEUES a job for each into the
-- existing jobs table, then advances next_run. `jobs_settings` holds the singleton
-- "🔔 Reminders" conversation id the scheduled results are injected into.
--
-- Idempotent (CREATE ... IF NOT EXISTS), schema-qualified to careeragent_jobs.
-- This is the same DDL init.sql applies on a fresh volume and store.ensure_schema
-- applies at startup on an existing volume; kept here as the numbered, replayable
-- migration of record. Seeding the DEFAULT schedule rows is done in application
-- code (store.seed_default_schedules, ON CONFLICT DO NOTHING) — NOT here — so the
-- cadence stays a config value, not a migration.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS careeragent_jobs;
SET search_path TO careeragent_jobs;

-- A recurring schedule: fire job `kind` (with `spec`) every `interval_seconds`.
-- `name` is a STABLE seed key (e.g. 'follow_up_scan') so re-seeding is idempotent
-- and an operator's later enable/disable or retune is never overwritten.
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

-- The scheduler scans only enabled, due rows, oldest first.
CREATE INDEX IF NOT EXISTS schedules_due
    ON schedules (next_run) WHERE enabled;

-- Singleton service settings (currently just reminders_conversation_id).
CREATE TABLE IF NOT EXISTS jobs_settings (
    key   text PRIMARY KEY,
    value text
);
