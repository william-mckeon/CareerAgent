-- careeragent-memory schema bootstrap.
--
-- Applied automatically on the memory-owned Postgres at first boot (mounted into
-- /docker-entrypoint-initdb.d/ by docker-compose). This is memory's OWN database
-- -- it is NOT the logger's shared instance. DDL lives here, matching how
-- careeragent-logger keeps its schema in init.sql; the service assumes the table
-- already exists and never issues DDL at runtime.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS careeragent_memory;

-- One row per stored conversation turn (a user input or an assistant output).
--
-- `embedding` is an UNSIZED vector: the dimensionality follows whatever the BYOC
-- embedding model emits, and search is exact cosine over a bounded per-session
-- set, so no ANN index (and therefore no fixed dimension) is required.
CREATE TABLE IF NOT EXISTS careeragent_memory.turns (
    id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id    TEXT         NOT NULL,
    role          TEXT         NOT NULL CHECK (role IN ('user', 'assistant')),
    content       TEXT         NOT NULL,
    content_hash  TEXT         NOT NULL,
    embedding     VECTOR       NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Retrieval always filters by session, so index it.
CREATE INDEX IF NOT EXISTS idx_turns_session
    ON careeragent_memory.turns (session_id);

-- Dedupe: re-ingesting an identical turn within a session is a no-op
-- (the service relies on this via ON CONFLICT DO NOTHING).
CREATE UNIQUE INDEX IF NOT EXISTS uq_turns_session_hash
    ON careeragent_memory.turns (session_id, content_hash);
