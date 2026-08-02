-- =============================================================================
-- careeragent-dossier migration 0004 — agent-authored durable preferences (P7 #17).
--
-- IDEMPOTENT. Safe to run against a LIVE database (CREATE IF NOT EXISTS only), so
-- it adds the preferences tier WITHOUT wiping the volume. init.sql carries the
-- same DDL for fresh installs.
--
-- preferences are user-STATED coaching preferences the coach pins each turn as
-- STANDING INSTRUCTIONS (e.g. "targets senior PM", "metric-first bullets", "one
-- page"). They are NOT career evidence: deliberately kept OUT of the grounding
-- corpus so a preference can never back an invented resume claim (ADR-002).
-- No search_vector — these are never full-text searched; they are injected whole.
--
-- Apply:
--   docker exec -i careeragent-dossier-db psql -U careeragent_dossier \
--     -d careeragent_dossier < database/migrations/0004_preferences.sql
-- =============================================================================

SET search_path TO careeragent_dossier;

CREATE TABLE IF NOT EXISTS preferences (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    content     text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS preferences_by_created ON preferences (created_at);
