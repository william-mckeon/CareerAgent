-- =============================================================================
-- careeragent-dossier migration 0003 — projects.commit_sha (review idempotency).
--
-- IDEMPOTENT. Safe to run against a LIVE database (ADD COLUMN IF NOT EXISTS), so
-- it adds the reviewed-HEAD-sha column WITHOUT wiping the volume — the saved
-- profile, applications, and projects are preserved. `init.sql` carries the
-- same column for fresh installs.
--
-- Purpose: careeragent-review records the repo HEAD sha it reviewed here, so a
-- later review can compare against the repo's current HEAD and SKIP the (costly)
-- re-review when nothing changed.
--
-- Apply:
--   docker exec -i careeragent-dossier-db psql -U careeragent_dossier \
--     -d careeragent_dossier < database/migrations/0003_projects_commit_sha.sql
-- =============================================================================

SET search_path TO careeragent_dossier;

ALTER TABLE projects ADD COLUMN IF NOT EXISTS commit_sha text;
