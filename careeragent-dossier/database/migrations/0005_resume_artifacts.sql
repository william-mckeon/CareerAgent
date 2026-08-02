-- =============================================================================
-- careeragent-dossier migration 0005 — rendered résumé artifacts (P7 #16).
--
-- IDEMPOTENT. Safe to run against a LIVE database (CREATE IF NOT EXISTS only), so
-- it adds the artifacts tier WITHOUT wiping the volume. init.sql carries the same
-- DDL for fresh installs.
--
-- resume_artifacts holds the BINARY rendered documents (PDF/DOCX bytes) produced
-- by careeragent-render from an application's tailored résumé. The bytes are
-- stored here — NOT streamed through the coach's tool result or the /chat SSE
-- content stream (which careeragent-sessions persists + replays) — and served
-- back on demand via GET /applications/{id}/artifact. One monotonic version per
-- application, mirroring resume_versions.
--
-- The ats_* columns are RESERVED for pairing an artifact with its ats_score
-- coverage ("here's your PDF; it covers 8/12 JD keywords"); render_resume leaves
-- them NULL today, so they are all nullable.
--
-- Apply:
--   docker exec -i careeragent-dossier-db psql -U careeragent_dossier \
--     -d careeragent_dossier < database/migrations/0005_resume_artifacts.sql
-- =============================================================================

SET search_path TO careeragent_dossier;

CREATE TABLE IF NOT EXISTS resume_artifacts (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id  uuid        NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    version         integer     NOT NULL,
    format          text        NOT NULL,           -- 'pdf' | 'docx'
    filename        text        NOT NULL,           -- suggested download name
    content         bytea       NOT NULL,           -- the raw document bytes
    byte_size       integer     NOT NULL,           -- len(content), a sanity check
    ats_score       integer,                        -- reserved (pairs with ats_score)
    ats_coverage    text,                           -- reserved, e.g. "8/12"
    ats_matched     jsonb,                          -- reserved
    ats_missing     jsonb,                          -- reserved
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (application_id, version)
);
CREATE INDEX IF NOT EXISTS resume_artifacts_by_application
    ON resume_artifacts (application_id, version DESC);
