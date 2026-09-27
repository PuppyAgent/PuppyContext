-- ============================================================
-- Phase 1: version_* table aliases over mut_* tables
-- ============================================================
--
-- The runtime architecture is "Version Engine"; the SQL layer
-- still carries the legacy ``mut_*`` prefix from the previous
-- mut/version refactor. This migration adds ``version_*`` views
-- over the existing ``mut_*`` tables so reads using the new
-- name work without touching production data.
--
-- The views are auto-updatable (single-table ``SELECT *`` with
-- no WHERE / DISTINCT / aggregates), so INSERT / UPDATE / DELETE
-- pass straight through to the base tables. RLS policies on the
-- base tables continue to govern access — auto-updatable views
-- in PostgreSQL invoke the base table's RLS rather than their own.
--
-- Scope of THIS migration (Phase 1, additive, fully reversible):
--   * CREATE VIEW version_* over mut_* tables.
--   * No table renames, no column renames, no RPC renames.
--   * Backend ``db_names.py`` keeps pointing at ``mut_*``.
--
-- Phase 2 (separate migration, requires DB review before applying):
--   * RENAME mut_* tables to version_* and drop these views.
--   * Recreate the compat layer the other way (mut_* views over
--     the now-canonical version_* tables) so any unreleased code
--     reading via the old name keeps working during the cutover.
--   * Rename the publish/claim/get_* RPC functions and update
--     their bodies to reference the new table names.
--   * Update ``backend/.../db_names.py`` constants.
--
-- Phase 3 (deferred, deploy-time decision):
--   * DROP the mut_* compat views.
--   * DROP the *_mut_* RPC wrappers.
--
-- ──────────────────────────────────────────────────────────────

BEGIN;

-- ── Tables ───────────────────────────────────────────────────

CREATE OR REPLACE VIEW public.version_commits AS
    SELECT * FROM public.mut_commits;

CREATE OR REPLACE VIEW public.version_scope_state AS
    SELECT * FROM public.mut_scope_state;

CREATE OR REPLACE VIEW public.version_view_commits AS
    SELECT * FROM public.mut_version_index;

CREATE OR REPLACE VIEW public.version_outbox AS
    SELECT * FROM public.mut_version_outbox;

CREATE OR REPLACE VIEW public.version_conflicts AS
    SELECT * FROM public.mut_conflicts;

CREATE OR REPLACE VIEW public.version_object_locations AS
    SELECT * FROM public.mut_object_locations;

-- ── Permissions ──────────────────────────────────────────────
-- Match the underlying tables: only the service_role + authenticated
-- role can read/write through the view; auto-updatable views still
-- defer to the base table's RLS for row-level filtering.

GRANT SELECT, INSERT, UPDATE, DELETE ON public.version_commits TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.version_scope_state TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.version_view_commits TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.version_outbox TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.version_conflicts TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.version_object_locations TO service_role;

GRANT SELECT ON public.version_commits TO authenticated;
GRANT SELECT ON public.version_scope_state TO authenticated;
GRANT SELECT ON public.version_view_commits TO authenticated;
GRANT SELECT ON public.version_outbox TO authenticated;
GRANT SELECT ON public.version_conflicts TO authenticated;
GRANT SELECT ON public.version_object_locations TO authenticated;

-- ── PostgREST cache notification ────────────────────────────
NOTIFY pgrst, 'reload schema';

COMMIT;
