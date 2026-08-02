-- =============================================================================
-- careeragent-sessions schema — runs once on first boot of an empty DB volume.
--
-- Everything lives in the `careeragent_sessions` schema (NOT bare public) so the
-- service can later share a single Postgres instance with logger/memory under
-- its own schema — a config-only switch (point SESSIONS_DB_HOST/NAME at it).
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS careeragent_sessions;
SET search_path TO careeragent_sessions;

-- A conversation: a stable id + the ordered transcript hangs off it.
CREATE TABLE IF NOT EXISTS conversations (
    id          uuid        PRIMARY KEY,
    title       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    metadata    jsonb       NOT NULL DEFAULT '{}'::jsonb
);

-- Messages within a conversation, totally ordered by idx.
CREATE TABLE IF NOT EXISTS messages (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid        NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    idx             integer     NOT NULL,
    role            text        NOT NULL,
    content         text        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, idx)
);

CREATE INDEX IF NOT EXISTS messages_by_conversation
    ON messages (conversation_id, idx);

CREATE INDEX IF NOT EXISTS conversations_by_updated
    ON conversations (updated_at DESC);

-- Run state for careeragent-api P4 (interactive channel). ONE active run per
-- conversation: the durable snapshot a paused/interrupted coach turn resumes from.
-- See specs/0002-run-state-suspend-resume.md. Also applied idempotently at service
-- startup (store.ensure_schema) so an existing DB volume gets it without a re-init.
CREATE TABLE IF NOT EXISTS run_state (
    conversation_id  uuid        PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    status           text        NOT NULL DEFAULT 'running',   -- running|paused|complete|interrupted
    snapshot         jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- {convo, step, plan, partial_drafts}
    pending_call_id  text,                                      -- set only while status='paused'
    pending_kind     text,                                      -- question | approval
    pending_payload  jsonb,                                     -- what the frontend renders
    steer_queue      jsonb       NOT NULL DEFAULT '[]'::jsonb,  -- steering messages drained between steps
    interrupt_requested boolean  NOT NULL DEFAULT false,        -- P4.5: stop the run at its next step
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS run_state_paused
    ON run_state (status) WHERE status = 'paused';
