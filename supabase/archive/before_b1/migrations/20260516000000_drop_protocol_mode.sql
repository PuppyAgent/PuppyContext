-- ============================================================================
-- Drop projects.protocol_mode
-- ============================================================================
-- The legacy MUT wire protocol has been removed. Git smart-HTTP is the only
-- external protocol surface and the per-project ``protocol_mode`` flag (which
-- used to admit ``git`` / ``mut`` / ``both``) is no longer consulted by any
-- code path. Removing the column makes the schema match the new model
-- described in docs/architecture/07-version-engine-supplement.md §3.
-- ============================================================================

BEGIN;

ALTER TABLE public.projects
    DROP CONSTRAINT IF EXISTS projects_protocol_mode_valid;

ALTER TABLE public.projects
    DROP COLUMN IF EXISTS protocol_mode;

COMMIT;
