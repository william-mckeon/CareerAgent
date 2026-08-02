-- =============================================================================
-- 0001_jobs.sql — the jobs table + claim/lookup indexes.
--
-- Idempotent (CREATE ... IF NOT EXISTS), schema-qualified to careeragent_jobs.
-- This is the same DDL init.sql applies on a fresh volume and store.ensure_schema
-- applies at startup on an existing volume; kept here as the numbered, replayable
-- migration of record.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS careeragent_jobs;
SET search_path TO careeragent_jobs;

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

CREATE INDEX IF NOT EXISTS jobs_claimable
    ON jobs (created_at) WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS jobs_by_conversation
    ON jobs (conversation_id, created_at DESC);
